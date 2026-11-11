"""Score corpus traces with one monitor (experiments E2 to E4).

    python tools/score_monitors.py --monitor rule:armour-baseline --set full
    python tools/score_monitors.py --monitor model:qwen3-1.7b-q8_0 --set primary --prefixes

Analysis sets (preregistration 0003): ``primary`` is the real-agent benign
runs plus natural and inserted violations; ``full`` is every corpus trace;
``dataset`` is the Armour dataset traces. Rule monitors run in process; model
monitors run through a llama-server started for the pinned model
(configs/models.json) and the capped, cached, ledger-logging client.

For each trace the output records the full-trace score and, with
``--prefixes``, the scores of every prefix ending at or after the first
violating step of a violation trace (for time to detection). Progress is
appended to ``runs/scores/<set>/<monitor>.partial.jsonl`` (resumable); on
completion the scores are written, sorted by trace id, to
``artifacts/scores/<set>/<monitor>.jsonl.gz`` (fixed gzip mtime) with a
``.meta.json`` holding provenance: corpus manifest hash, monitor and prompt
hash, model pin, settings, timings, and the command line.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("BASERATE_ROOT", REPO))
"""Data root; tests point it at a temporary tree."""
sys.path.insert(0, str(REPO / "tools"))

from baserate.backends.client import ModelClient  # noqa: E402
from baserate.monitors import model as model_mon  # noqa: E402
from baserate.monitors.rules import builtin_rule_monitors  # noqa: E402
from baserate.traces.schema import Trace  # noqa: E402
from chainlib import sha256_obj  # noqa: E402
from run_agents import model_spec, repair_jsonl, start_server, stop_server  # noqa: E402

E2_PREREG = "prereg/0003-e2-main-study.md"

SETS = ("primary", "full", "dataset")

# One usage entry per this many seconds of model scoring, and one when the run ends
# (normally or on an exception). An entry every few calls made the tracked ledger
# change every minute of a long run; local calls have no count cap (budget.toml),
# so only the entries' granularity changes.
LEDGER_FLUSH_SECONDS = 900.0


class LedgerFlusher:
    """Flushes a client's usage to the ledger at most once per ``seconds``."""

    def __init__(
        self,
        client: Any,
        seconds: float = LEDGER_FLUSH_SECONDS,
        clock: Any = time.monotonic,
    ) -> None:
        self.client, self.seconds, self.clock = client, seconds, clock
        self.last = clock()

    def maybe_flush(self) -> bool:
        """Flush if the interval has passed since the last flush; report whether it did."""
        now = self.clock()
        if now - self.last < self.seconds:
            return False
        self.client.flush()
        self.last = now
        return True


def prereg_link(path: str) -> dict[str, str]:
    """A citation of a preregistration: its path and its config's canonical SHA-256."""
    text = (ROOT / path).read_text(encoding="utf-8")
    config = json.loads(re.findall(r"```json prereg-config\n(.*?)```", text, re.S)[0])
    return {"prereg": path, "prereg_config_sha256": sha256_obj(config)}


def in_set(trace: Trace, name: str) -> bool:
    """Whether a corpus trace belongs to an analysis set."""
    if name == "full":
        return True
    if name == "dataset":
        return trace.source == "dataset"
    real_benign = trace.label == "benign" and trace.source == "real_agent"
    real_violation = trace.label == "violation" and trace.violation_source in {
        "natural",
        "inserted",
    }
    return real_benign or real_violation


def corpus_traces(manifest_path: Path) -> Iterable[Trace]:
    """Every trace in the corpus shards, in manifest order."""
    manifest = json.loads(manifest_path.read_text())
    for shard in manifest["shards"]:
        path = (
            ROOT / shard["path"] if not Path(shard["path"]).is_absolute() else Path(shard["path"])
        )
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != shard["sha256"]:
            raise SystemExit(f"shard hash mismatch: {shard['path']}")
        for line in gzip.decompress(data).decode("utf-8").splitlines():
            if line.strip():
                yield Trace.from_dict(json.loads(line))


def prefix_start(trace: Trace) -> int | None:
    """First action index whose prefix is scored for time to detection, or None."""
    if trace.label != "violation" or trace.first_violating_step is None:
        return None
    return max(int(trace.first_violating_step), 1)


def model_record(mon: Any, trace: Trace, start: int | None) -> dict[str, Any]:
    """One model monitor's score record for a trace.

    ``seconds`` times the full-trace call alone, as for rule monitors, because
    the E4 cost (prereg/0005) is its median; prefix calls are timed apart in
    ``prefix_seconds``.
    """
    t0 = time.monotonic()
    s = mon.score(trace)
    rec: dict[str, Any] = {
        "trace_id": trace.trace_id,
        "score": s.score,
        "digit_mass": s.details.get("digit_mass"),
        "seconds": round(time.monotonic() - t0, 3),
        # a reply served from the response cache took no model time; E4's cost
        # (tools/analyze.py median_seconds) leaves such records out
        "cached": bool(s.details.get("cached", False)),
    }
    if start is not None:
        t1 = time.monotonic()
        rec["prefix_start"] = start
        rec["prefix_scores"] = mon.score_prefixes(trace, start)
        rec["prefix_seconds"] = round(time.monotonic() - t1, 3)
    return rec


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--monitor", required=True, help="rule:<pack> or model:<model key>")
    ap.add_argument("--set", dest="set_name", choices=SETS, required=True)
    ap.add_argument("--corpus", type=Path, default=ROOT / "artifacts" / "corpus" / "manifest.json")
    ap.add_argument("--prefixes", action="store_true", help="also score prefixes of violations")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--limit", type=int, default=0, help="score only the first N traces (pilot)")
    args = ap.parse_args(argv)

    confirmatory_corpus = (ROOT / "artifacts" / "corpus" / "manifest.json").resolve()
    if args.limit and args.corpus.resolve() == confirmatory_corpus:
        raise SystemExit("a --limit pilot must use a pilot corpus, not the confirmatory one")
    manifest_bytes = args.corpus.read_bytes()
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    traces = [t for t in corpus_traces(args.corpus) if in_set(t, args.set_name)]
    traces.sort(key=lambda t: t.trace_id)
    if args.limit:
        traces = traces[: args.limit]
    safe = args.monitor.replace(":", "_")
    out_dir = ROOT / "artifacts" / "scores" / args.set_name
    banked = out_dir / f"{safe}.meta.json"
    if not args.limit and banked.exists() and (out_dir / f"{safe}.jsonl.gz").exists():
        old = json.loads(banked.read_text())
        same = (old.get("corpus_manifest_sha256"), old.get("prefixes"), old.get("traces"))
        gz = hashlib.sha256((out_dir / f"{safe}.jsonl.gz").read_bytes()).hexdigest()
        if same == (manifest_sha, args.prefixes, len(traces)) and old.get("scores_sha256") == gz:
            print(f"{args.monitor} on {args.set_name}: already banked; nothing to do", flush=True)
            return 0
    work = ROOT / "runs" / "scores" / args.set_name
    work.mkdir(parents=True, exist_ok=True)
    partial = work / f"{safe}{'.limit' if args.limit else ''}.partial.jsonl"
    done: dict[str, dict[str, Any]] = {}
    # a resume may reuse partial records only for the same corpus and settings
    sidecar = partial.with_suffix(".settings.json")
    settings = {"corpus_manifest_sha256": manifest_sha, "prefixes": args.prefixes}
    if partial.exists() and sidecar.exists() and json.loads(sidecar.read_text()) != settings:
        raise SystemExit(
            f"{partial} was scored with other settings or another corpus; move it away to rescore"
        )
    work.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(settings) + "\n")
    if repair_jsonl(partial):
        print(f"cut a partial last record from {partial}", flush=True)
    if partial.exists():
        for line in partial.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["trace_id"]] = rec
    lock = threading.Lock()
    server = None
    meta: dict[str, Any] = {
        "monitor": args.monitor,
        "set": args.set_name,
        "confirmatory": True,
        **prereg_link(E2_PREREG),
    }
    started = time.monotonic()

    if args.monitor.startswith("rule:"):
        monitors = {m.name: m for m in builtin_rule_monitors()}
        if args.monitor not in monitors:
            raise SystemExit(f"unknown rule monitor; known: {sorted(monitors)}")
        rule = monitors[args.monitor]
        meta["family"] = "rule"

        def score(trace: Trace) -> dict[str, Any]:
            t0 = time.monotonic()
            s = rule.score(trace)
            elapsed = time.monotonic() - t0
            rec: dict[str, Any] = {
                "trace_id": trace.trace_id,
                "score": s.score,
                "flagged_step": s.flagged_step,
                "seconds": round(elapsed, 6),
            }
            start = prefix_start(trace) if args.prefixes else None
            if start is not None:
                rec["prefix_start"] = start
                rec["prefix_scores"] = [
                    rule.score_view(trace.trace_id, trace.monitor_view(upto=i + 1)).score
                    for i in range(start, len(trace.actions))
                ]
            return rec

        concurrency = 1
    elif args.monitor.startswith("model:"):
        key = args.monitor.split(":", 1)[1]
        spec, entry = model_spec(key, args.port)
        client = ModelClient(spec, ROOT, purpose=f"score:{args.set_name}")
        server = start_server(
            entry, args.port, args.concurrency, args.threads, work / f"{safe}.server.log"
        )
        meta["server_command"] = [str(a) for a in server.args]  # type: ignore[union-attr]  # Popen.args is the list start_server passed
        mon = model_mon.ModelMonitor(args.monitor, client)
        meta.update(
            {
                "family": "model",
                "model": key,
                "hf_repo": entry["hf_repo"],
                "revision": entry["revision"],
                "sha256": entry["sha256"],
                "quant": entry["quant"],
                "params_b": entry["params_b"],
                "prompt_sha256": model_mon.prompt_sha256(),
                "n_probs": mon.n_probs,
            }
        )

        def score(trace: Trace) -> dict[str, Any]:
            return model_record(mon, trace, prefix_start(trace) if args.prefixes else None)

        concurrency = args.concurrency
    else:
        raise SystemExit("monitor must start with rule: or model:")

    todo = [t for t in traces if t.trace_id not in done]
    print(
        f"{args.monitor} on {args.set_name}: {len(traces)} traces, {len(todo)} to score", flush=True
    )

    def job(trace: Trace) -> None:
        rec = score(trace)
        with lock, partial.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        done[trace.trace_id] = rec

    flusher = LedgerFlusher(client) if server is not None else None
    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for i, fut in enumerate([pool.submit(job, t) for t in todo], start=1):
                fut.result()
                if flusher is not None:
                    flusher.maybe_flush()
                if i % 100 == 0:
                    print(f"  {i}/{len(todo)}", flush=True)
    finally:
        if server is not None:
            try:
                stop_server(server)
            finally:
                client.flush()
    if args.limit:
        print("pilot (--limit): nothing banked", flush=True)
        return 0
    records = [done[t.trace_id] for t in traces]
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = (
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records) + "\n"
    ).encode()
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(raw)
    out = out_dir / f"{safe}.jsonl.gz"
    out.write_bytes(buf.getvalue())
    meta.update(
        {
            "scores_file": str(out.relative_to(ROOT)),
            "scores_sha256": hashlib.sha256(buf.getvalue()).hexdigest(),
            "traces": len(records),
            "corpus_manifest_sha256": manifest_sha,
            "prefixes": args.prefixes,
            "concurrency": concurrency,
            "threads": args.threads,
            "wall_seconds_this_session": round(time.monotonic() - started, 1),
            "scored_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "git_sha": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
            ).stdout.strip(),
            "command": " ".join(sys.argv),
            "cap_share": os.environ.get("CAP_SHARE", "1"),
            "usage_note": "local model calls are logged to artifacts/usage/ledger.jsonl",
        }
    )
    (out_dir / f"{safe}.meta.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(f"banked {out.relative_to(ROOT)} ({len(records)} traces)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
