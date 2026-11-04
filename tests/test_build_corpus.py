"""Tests for the corpus builder (tools/build_corpus.py) on scripted runs."""

from __future__ import annotations

import gzip
import importlib.util
import json
import subprocess
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
    none = ["--planned-insertions", "0"]  # this build has no insertion runs
    assert bc.main(["--runs", str(runs), "--variants", "3", "--out", str(out1), *none]) == 0
    assert bc.main(["--runs", str(runs), "--variants", "3", "--out", str(out2), *none]) == 0
    m1 = json.loads((out1 / "manifest.json").read_text())
    m2 = json.loads((out2 / "manifest.json").read_text())
    assert [s["sha256"] for s in m1["shards"]] == [s["sha256"] for s in m2["shards"]]
    comp = m1["composition"]
    assert comp["benign_real_agent"] == 5
    assert m1["relabeling"]["replayed"] == 5 and m1["relabeling"]["inserted_dropped"] == 0
    assert comp["by_source"]["procedural"] == 15
    assert comp["by_source"]["dataset"] > 0
    records = []
    for shard in m1["shards"]:
        with gzip.open(shard["path"], "rt") as fh:
            records += [json.loads(line) for line in fh]
    assert len(records) == comp["traces"] == sum(s["records"] for s in m1["shards"])
    # the canary is in the manifest and in every record, and readers drop it
    assert m1["canary"] == bc.CANARY and "canary GUID" in bc.CANARY
    assert all(r["canary"] == bc.CANARY for r in records)
    traces = [Trace.from_dict(r) for r in records]
    assert all(
        "canary" not in t.to_dict() and bc.CANARY not in json.dumps(t.monitor_view())
        for t in traces
    )
    assert len({t.trace_id for t in traces}) == len(traces)
    # variants stay in their source run's cluster; Armour traces are grouped (D12)
    real_runs = {t.source_run_id for t in traces if t.source == "real_agent"}
    assert {t.source_run_id for t in traces if t.source == "procedural"} <= real_runs
    assert all(t.source_run_id.startswith("armour:") for t in traces if t.source == "dataset")
    assert comp["independent_source_runs"] < comp["traces"]
    # the corpus cites its protocol and both amendments, with their config hashes
    assert m1["confirmatory"] is True and m1["prereg"] == "prereg/0002-e1-corpus.md"
    assert [a["prereg"] for a in m1["prereg_amendments"]] == [
        "prereg/0006-e1-amendment-oracle.md",
        "prereg/0007-e1-amendment-insertion.md",
    ]
    for citation in [m1, *m1["prereg_amendments"]]:
        expected = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "prereg.py"), "hash", citation["prereg"]],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert citation["prereg_config_sha256"] == expected
    # the stopping rule's outcome: realized seeds per subject model
    assert m1["task_seeds"] == [{"agent": "m", "runs": 5, "distinct_seeds": 5, "ranges": "0 to 4"}]
    assert sum(m1["armour"]["label_sources"].values()) == comp["by_source"]["dataset"]


def test_seed_ranges_render_gaps_and_singletons() -> None:
    assert bc.seed_ranges([3, 0, 1, 2, 350, 351, 9]) == "0 to 3, 9, 350 to 351"
    assert bc.seed_ranges([]) == ""


def test_pilot_seeds_are_refused(tmp_path: Path) -> None:
    runs = _run_file(tmp_path, range(900_000, 900_001))
    with pytest.raises(SystemExit, match="pilot trace"):
        bc.main(["--runs", str(runs), "--out", str(tmp_path / "c"), "--planned-insertions", "0"])


def _insert_dir(tmp_path: Path, runs: Path, attempts: int, skips: list[dict[str, object]]) -> Path:
    """An insertion run directory: inserted traces, its meta.json, and its skip log."""
    domain = get_domain("customer_support")
    sources = [Trace.from_dict(json.loads(line)) for line in runs.read_text().splitlines()]
    out = tmp_path / "insert-m"
    out.mkdir()
    lines = []
    for src in sources[:2]:
        ep = runner.run_inserted(Scripted([]), domain, src, 1, "audit_log_edit", seed=1)
        trace = runner.to_trace(
            ep,
            trace_id=f"ins:{src.trace_id}:1:audit_log_edit",
            agent_id="m",
            source="inserted",
            source_run_id=src.source_run_id,
            synthetic=True,
            provenance={"task_seed": src.provenance["task_seed"], "inserted_at": 1},
            inserted_type="audit_log_edit",
        )
        lines.append(json.dumps(trace.to_dict()))
    (out / "traces.jsonl").write_text("\n".join(lines) + "\n")
    (out / "meta.json").write_text(
        json.dumps({"insert": True, "insertion": {"attempts": attempts}})
    )
    (out / "skipped.jsonl").write_text("".join(json.dumps(s) + "\n" for s in skips))
    return out / "traces.jsonl"


SKIP = {
    "trace_id": "ins:x:3:false_completion",
    "source": "x",
    "k": 3,
    "violation_type": "false_completion",
    "reason": runner.NO_USABLE_SUMMARY,
}


def test_insertion_attempts_and_skips_are_banked(tmp_path: Path) -> None:
    runs = _run_file(tmp_path, range(5))
    inserted = _insert_dir(tmp_path, runs, 3, [SKIP])
    out = tmp_path / "c"
    args = ["--runs", str(runs), str(inserted), "--variants", "1", "--out", str(out)]
    assert bc.main([*args, "--planned-insertions", "3"]) == 0
    log = json.loads((out / "manifest.json").read_text())["insertion"]
    assert log["attempts"] == 3 and log["inserted"] == 2
    assert log["skipped"] == {runner.NO_VALID_CALL: 0, runner.NO_USABLE_SUMMARY: 1}
    by_type = {r["type"]: r for r in log["by_type"]}
    assert by_type["audit_log_edit"]["inserted"] == 2 == by_type["audit_log_edit"]["attempts"]
    assert by_type["false_completion"][runner.NO_USABLE_SUMMARY] == 1
    assert by_type["false_completion"]["attempts"] == 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert any(i["path"].endswith("skipped.jsonl") for i in manifest["inputs"])


@pytest.mark.parametrize(
    ("attempts", "skips", "needle"),
    [
        (4, [SKIP], "insertion run incomplete"),
        (3, [{**SKIP, "reason": "made up"}], "unknown reason"),
    ],
    ids=["incomplete", "unknown-reason"],
)
def test_insertion_log_refuses_an_inconsistent_run(
    tmp_path: Path, attempts: int, skips: list[dict[str, object]], needle: str
) -> None:
    runs = _run_file(tmp_path, range(5))
    inserted = _insert_dir(tmp_path, runs, attempts, skips)
    with pytest.raises(SystemExit, match=needle):
        bc.main(
            ["--runs", str(runs), str(inserted), "--variants", "1", "--out", str(tmp_path / "c")]
        )


def test_a_build_without_the_planned_insertions_is_refused(tmp_path: Path) -> None:
    runs = _run_file(tmp_path, range(3))
    with pytest.raises(SystemExit, match="the protocol plans 700"):
        bc.main(["--runs", str(runs), "--variants", "1", "--out", str(tmp_path / "c")])
