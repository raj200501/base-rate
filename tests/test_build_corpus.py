"""Tests for the corpus builder (tools/build_corpus.py) on scripted runs."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from baserate.traces import runner
from baserate.traces.domains import get_domain
from baserate.traces.schema import Trace

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_corpus", ROOT / "tools" / "build_corpus.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_corpus"] = mod
    spec.loader.exec_module(mod)
    return mod


bc = _load()


class Scripted:
    def __init__(self, replies: list[str]):
        self.replies = replies

    def chat(self, messages: object, **kw: object) -> dict[str, object]:
        text = self.replies.pop(0) if self.replies else '{"tool": "task.finish", "args": {}}'
        return {"content": text, "completion_tokens": 1}


def _run_file(tmp_path: Path, seeds: range) -> Path:
    domain = get_domain("customer_support")
    lines = []
    for seed in seeds:
        task, _ = domain.make_task(seed)
        tid = next(e for r, e in task.write_scope if r == "ticket")
        replies = [
            json.dumps({"tool": "tickets.get", "args": {"id": tid}}),
            json.dumps(
                {
                    "tool": "task.finish",
                    "args": {"status": "incomplete", "summary": "Need more info."},
                }
            ),
        ]
        ep = runner.run_episode(Scripted(replies), domain, seed, seed=seed)
        trace = runner.to_trace(
            ep,
            trace_id=f"run:m:customer_support:{seed}",
            agent_id="m",
            source="real_agent",
            source_run_id=f"run:m:customer_support:{seed}",
            synthetic=False,
            provenance={"task_seed": seed},
        )
        lines.append(json.dumps(trace.to_dict()))
    path = tmp_path / "traces.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return path


def test_build_is_deterministic_and_complete(tmp_path: Path) -> None:
    runs = _run_file(tmp_path, range(5))
    out1, out2 = tmp_path / "c1", tmp_path / "c2"
    assert bc.main(["--runs", str(runs), "--variants", "3", "--out", str(out1)]) == 0
    assert bc.main(["--runs", str(runs), "--variants", "3", "--out", str(out2)]) == 0
    m1 = json.loads((out1 / "manifest.json").read_text())
    m2 = json.loads((out2 / "manifest.json").read_text())
    assert [s["sha256"] for s in m1["shards"]] == [s["sha256"] for s in m2["shards"]]
    comp = m1["composition"]
    assert comp["benign_real_agent"] == 5
    assert comp["by_source"]["procedural"] == 15
    assert comp["by_source"]["dataset"] > 0
    records = []
    for shard in m1["shards"]:
        with gzip.open(shard["path"], "rt") as fh:
            records += [json.loads(line) for line in fh]
    assert len(records) == comp["traces"] == sum(s["records"] for s in m1["shards"])
    traces = [Trace.from_dict(r) for r in records]
    assert len({t.trace_id for t in traces}) == len(traces)
    # variants stay in their source run's cluster; Armour traces are grouped (D12)
    real_runs = {t.source_run_id for t in traces if t.source == "real_agent"}
    assert {t.source_run_id for t in traces if t.source == "procedural"} <= real_runs
    assert all(t.source_run_id.startswith("armour:") for t in traces if t.source == "dataset")
    assert comp["independent_source_runs"] < comp["traces"]


def test_pilot_seeds_are_refused(tmp_path: Path) -> None:
    runs = _run_file(tmp_path, range(900_000, 900_001))
    with pytest.raises(SystemExit, match="pilot trace"):
        bc.main(["--runs", str(runs), "--out", str(tmp_path / "c")])
