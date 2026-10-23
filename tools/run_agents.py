"""Run subject agents over seeded tasks with a local model (corpus building).

    python tools/run_agents.py --model qwen3-1.7b-q8_0 --seeds 0:300 --out runs/agents/batch1

Starts a llama-server for the pinned model (configs/models.json; weights under
$BASERATE_MODELS, default ~/models), runs one episode per (domain, task seed)
with ``--concurrency`` parallel slots, and appends each labeled trace as a
JSON line to ``<out>/traces.jsonl``. Resumable: episodes already in the file
are skipped. Writes ``<out>/run.pid`` and ``<out>/meta.json``; usage goes to
the ledger through the model client. Seeds 900000 and above are reserved for
pilots and dry runs, which never enter the corpus.

``--insert`` switches to controlled insertion: it reads benign real-agent
traces from ``--from`` files and writes inserted-violation continuations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from baserate.backends.client import ModelClient, ModelSpec
from baserate.traces import runner
from baserate.traces.domains import DOMAINS, get_domain
from baserate.traces.schema import VIOLATION_TYPES, Trace, ViolationType

ROOT = Path(__file__).resolve().parents[1]
PILOT_SEED_FLOOR = 900_000
LLAMA_SERVER = Path(
    os.environ.get("LLAMA_SERVER", "/home/user/opt/llama.cpp/build/bin/llama-server")
)


def model_spec(key: str, port: int) -> tuple[ModelSpec, dict[str, Any]]:
    """The pinned model entry and a client spec for it."""
    entry: dict[str, Any] = json.loads((ROOT / "configs" / "models.json").read_text())["models"][
        key
    ]
    return ModelSpec(
        key=key, params_b=float(entry["params_b"]), endpoint=f"http://127.0.0.1:{port}"
    ), entry


def start_server(
    entry: dict[str, Any], port: int, slots: int, threads: int, log: Path
) -> subprocess.Popen[bytes]:
    """Start llama-server and wait until it answers."""
    weights = Path(os.environ.get("BASERATE_MODELS", Path.home() / "models")) / entry["file"]
    cmd = [
        str(LLAMA_SERVER),
        "-m",
        str(weights),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "-t",
        str(threads),
        "-np",
        str(slots),
        "-c",
        str(slots * 6144),
        "--jinja",
    ]
    proc = subprocess.Popen(cmd, stdout=log.open("ab"), stderr=subprocess.STDOUT)
    for _ in range(240):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except OSError:
            time.sleep(1)
    proc.terminate()
    raise SystemExit("llama-server did not become healthy")


def episode_seed(sample_seed: int, trace_id: str) -> int:
    """Deterministic sampling seed for one episode."""
    return int(hashlib.sha256(f"{sample_seed}:{trace_id}".encode()).hexdigest()[:8], 16)


def done_ids(path: Path) -> set[str]:
    """Trace ids already written."""
    if not path.exists():
        return set()
    return {json.loads(line)["trace_id"] for line in path.read_text().splitlines() if line.strip()}


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--domains", default=",".join(sorted(DOMAINS)))
    ap.add_argument("--seeds", default="0:10", help="task seed range START:END")
    ap.add_argument("--sample-seed", type=int, default=20261023)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-server", action="store_true", help="use an already running server")
    ap.add_argument("--pilot", action="store_true", help="allow pilot seeds (>= 900000) only")
    ap.add_argument("--insert", action="store_true", help="controlled-insertion mode")
    ap.add_argument("--from", dest="sources", type=Path, nargs="*", default=[])
    ap.add_argument("--max-inserts", type=int, default=0)
    args = ap.parse_args(argv)

    start, end = (int(x) for x in args.seeds.split(":"))
    if args.pilot != (start >= PILOT_SEED_FLOOR):
        raise SystemExit("pilot runs must use seeds >= 900000 and corpus runs seeds below it")
    args.out.mkdir(parents=True, exist_ok=True)
    out_file = args.out / "traces.jsonl"
    (args.out / "run.pid").write_text(str(os.getpid()))
    spec, entry = model_spec(args.model, args.port)
    meta = {
        "model": args.model,
        "hf_repo": entry["hf_repo"],
        "revision": entry["revision"],
        "file": entry["file"],
        "sha256": entry["sha256"],
        "runtime": json.loads((ROOT / "configs" / "models.json").read_text())["runtime"],
        "system_template_sha256": hashlib.sha256(runner.SYSTEM_TEMPLATE.encode()).hexdigest(),
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "max_steps": runner.MAX_STEPS,
        "sample_seed": args.sample_seed,
        "seeds": args.seeds,
        "insert": args.insert,
        "command": " ".join(sys.argv),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip(),
    }
    (args.out / "meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    server = (
        None
        if args.no_server
        else start_server(entry, args.port, args.concurrency, args.threads, args.out / "server.log")
    )
    client = ModelClient(spec, ROOT, purpose="agent-runs" if not args.insert else "insertions")
    lock = threading.Lock()
    seen = done_ids(out_file)
    provenance_base = {
        k: meta[k] for k in ("model", "revision", "sha256", "sample_seed", "git_sha")
    }

    def write(trace: Trace) -> None:
        with lock, out_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(trace.to_dict(), sort_keys=True) + "\n")

    def real_job(domain_name: str, task_seed: int) -> None:
        trace_id = f"run:{args.model}:{domain_name}:{task_seed}"
        if trace_id in seen:
            return
        sseed = episode_seed(args.sample_seed, trace_id)
        ep = runner.run_episode(client, get_domain(domain_name), task_seed, seed=sseed)
        write(
            runner.to_trace(
                ep,
                trace_id=trace_id,
                agent_id=args.model,
                source="real_agent",
                source_run_id=trace_id,
                synthetic=False,
                provenance={**provenance_base, "task_seed": task_seed, "episode_seed": sseed},
            )
        )

    def insert_job(src: Trace, k: int, vtype: ViolationType) -> None:
        trace_id = f"ins:{src.trace_id}:{k}:{vtype}"
        if trace_id in seen:
            return
        sseed = episode_seed(args.sample_seed, trace_id)
        ep = runner.run_inserted(client, get_domain(src.domain), src, k, vtype, seed=sseed)
        if ep is None:
            return
        write(
            runner.to_trace(
                ep,
                trace_id=trace_id,
                agent_id=src.agent_id,
                source="inserted",
                source_run_id=src.source_run_id,
                synthetic=True,
                provenance={
                    **provenance_base,
                    "task_seed": src.provenance["task_seed"],
                    "episode_seed": sseed,
                    "inserted_from": src.trace_id,
                    "inserted_at": k,
                },
                inserted_type=vtype,
            )
        )

    jobs: list[tuple[Any, ...]] = []
    if args.insert:
        sources: list[Trace] = []
        for f in args.sources:
            for line in f.read_text().splitlines():
                t = Trace.from_dict(json.loads(line))
                if t.label == "benign" and t.source == "real_agent" and t.agent_id == args.model:
                    sources.append(t)
        rng = random.Random(f"plan:{args.sample_seed}")
        rng.shuffle(sources)
        for i, src in enumerate(sources[: args.max_inserts or len(sources)]):
            last = len(src.actions) - 1  # agent steps are 1..last
            k = rng.randint(1, max(1, last))
            vtype = cast(ViolationType, VIOLATION_TYPES[i % len(VIOLATION_TYPES)])
            jobs.append((insert_job, src, k, vtype))
    else:
        for task_seed in range(start, end):
            for d in args.domains.split(","):
                jobs.append((real_job, d, task_seed))
    print(f"{len(jobs)} jobs ({len(seen)} already done)", flush=True)
    finished = 0
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for fut in [pool.submit(j[0], *j[1:]) for j in jobs]:
                fut.result()
                finished += 1
                if finished % 25 == 0:
                    client.flush()
                    print(f"{finished}/{len(jobs)} done", flush=True)
    finally:
        client.flush()
        if server is not None:
            server.terminate()
            server.wait(timeout=30)
    print(f"finished {finished} jobs", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
