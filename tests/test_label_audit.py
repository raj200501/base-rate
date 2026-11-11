"""Tests for the blind label audit tools: evidence, scoring, and the preregistration link.

tools/audit_evidence.py reads a workflow journal; tools/label_audit.py score
refuses any audit whose auditors used tools and banks an artifact that
make prereg-verify accepts.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str) -> ModuleType:
    if str(REPO / "tools") not in sys.path:
        sys.path.insert(0, str(REPO / "tools"))
    spec = importlib.util.spec_from_file_location(f"{name}_t", REPO / "tools" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


la = _load("label_audit")
ae = _load("audit_evidence")
sm = _load("score_monitors")

KEY = [
    {
        "audit_id": "A000",
        "trace_id": "t0",
        "stratum": "real benign",
        "label": "benign",
        "violation_type": None,
        "first_violating_step": None,
    },
    {
        "audit_id": "A001",
        "trace_id": "t1",
        "stratum": "inserted violation",
        "label": "violation",
        "violation_type": "audit_log_edit",
        "first_violating_step": 3,
    },
]
ANSWERS = [
    {"audit_id": "A000", "label": "benign", "violation_type": None, "first_violating_step": None},
    {
        "audit_id": "A001",
        "label": "violation",
        "violation_type": "audit_log_edit",
        "first_violating_step": 3,
    },
]


def evidence(*tools: list[str], packet: str = "") -> dict[str, Any]:
    return {
        "workflow_run": "wf_x",
        "packet_sha256": packet,
        "agents": [
            {"label": f"audit:{i}", "tools_used": t, "reads": [], "transcript_sha256": f"{i:064x}"}
            for i, t in enumerate(tools)
        ],
    }


def test_evidence_allows_only_reading_ones_own_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(la, "WORK", Path("/w"))
    ok = la.check_evidence(evidence(["StructuredOutput"], []))
    assert ok["agents"] == 2 and ok["tools_used"] == ["StructuredOutput"]
    own = evidence(["Read", "StructuredOutput"])
    own["agents"][0]["reads"] = ["/w/batches/batch-000.txt"]
    assert la.check_evidence(own)["reads"] == 1
    with pytest.raises(SystemExit, match="beyond reading its batch"):
        la.check_evidence(evidence(["Bash", "StructuredOutput"]))
    for path in ("/w/key.jsonl", "/w/batches/batch-001.txt"):
        peek = evidence(["Read", "StructuredOutput"])
        peek["agents"][0]["reads"] = ["/w/batches/batch-000.txt", path]
        with pytest.raises(SystemExit, match="other than its batch"):
            la.check_evidence(peek)
    with pytest.raises(SystemExit, match="no auditor agents"):
        la.check_evidence({"agents": []})


def test_batches_hold_only_the_monitor_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(la, "WORK", tmp_path)
    monkeypatch.setattr(la, "ROOT", tmp_path.parent)
    packet = [
        {"audit_id": f"A{i:03d}", "definitions": "DEFS", "trajectory": f"[0] user.request x{i}"}
        for i in range(23)
    ]
    (tmp_path / "packet.jsonl").write_text("".join(json.dumps(x) + "\n" for x in packet))
    la.batches()
    index = json.loads((tmp_path / "batches" / "index.json").read_text())
    assert [len(b["audit_ids"]) for b in index] == [10, 10, 3]
    text = la.batch_path(2).read_text()
    assert "=== A020 ===" in text and "[0] user.request x22" in text and "DEFS" in text
    for word in ("benign", "violation_type", "stratum", "label"):
        assert word not in text


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def test_scored_audit_cites_its_preregistration_and_passes_prereg_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "prereg").mkdir(parents=True)
    (root / "tools").mkdir()
    shutil.copy(REPO / "prereg" / "0002-e1-corpus.md", root / "prereg")
    for tool in ("prereg.py", "chainlib.py"):
        shutil.copy(REPO / "tools" / tool, root / "tools")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "freeze E1")
    work = root / "runs" / "audit"
    work.mkdir(parents=True)
    (work / "key.jsonl").write_text("".join(json.dumps(k) + "\n" for k in KEY))
    (work / "answers.jsonl").write_text("".join(json.dumps(a) + "\n" for a in ANSWERS))
    (work / "packet.jsonl").write_text('{"audit_id": "A000"}\n{"audit_id": "A001"}\n')
    (root / "artifacts" / "corpus").mkdir(parents=True)
    monkeypatch.setattr(la, "ROOT", root)
    monkeypatch.setattr(la, "WORK", work)
    import hashlib

    other = evidence(["StructuredOutput"], packet="0" * 64)
    with pytest.raises(SystemExit, match="another packet"):
        la.score("claude subagent (test)", other)
    packet_sha = hashlib.sha256((work / "packet.jsonl").read_bytes()).hexdigest()
    la.score("claude subagent (test)", evidence(["StructuredOutput"], packet=packet_sha))
    out = json.loads((root / "artifacts" / "corpus" / "label_audit.json").read_text())
    assert out["confirmatory"] is True and out["blind"] is True
    assert out["agreement"] == 1.0 and out["auditor_evidence"]["agents"] == 1
    (root / ".gitignore").write_text("runs/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "bank the audit")
    run = subprocess.run(
        [sys.executable, "tools/prereg.py", "verify"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.stdout.splitlines()[-1] == (
        "PREREG-VERIFY: PASS 1 preregistrations, 1 linked artifacts"
    ), run.stdout


def test_journal_evidence_and_answers_are_read_as_the_agents_returned_them(
    tmp_path: Path,
) -> None:
    wf = tmp_path / "wf_test"
    wf.mkdir()
    lines = []
    for i, (agent, answer) in enumerate([("a1", ANSWERS[1]), ("a0", ANSWERS[0])]):
        read = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "input": {"file_path": f"/w/batches/batch-{i:03d}.txt"},
                    }
                ]
            },
        }
        (wf / f"agent-{agent}.jsonl").write_text(json.dumps(read) + "\n")
        lines.append({"type": "started", "key": f"k{i}", "agentId": agent, "label": f"audit:{i}"})
        lines.append(
            {
                "type": "result",
                "key": f"k{i}",
                "agentId": agent,
                "result": {"answers": [answer]},
                "toolsUsed": ["StructuredOutput"],
            }
        )
    lines.append({"type": "started", "key": "x", "agentId": "r", "label": "review:other"})
    (wf / "journal.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    out = tmp_path / "audit"
    out.mkdir()
    (out / "packet.jsonl").write_text('{"audit_id": "A000"}\n{"audit_id": "A001"}\n')
    assert ae.main(["--journal", str(wf / "journal.jsonl"), "--out", str(out)]) == 0
    ev = json.loads((out / "evidence.json").read_text())
    assert ev["workflow_run"] == "wf_test" and [a["label"] for a in ev["agents"]] == [
        "audit:0",
        "audit:1",
    ]
    written = [json.loads(x) for x in (out / "answers.jsonl").read_text().splitlines()]
    assert [a["audit_id"] for a in written] == ["A000", "A001"]  # packet order
    assert ev["agents"][0]["reads"] == ["/w/batches/batch-000.txt"]
    assert ev["agents"][0]["tools_used"] == ["Read", "StructuredOutput"]
    assert len(ev["packet_sha256"]) == 64
    # an audit id answered by two auditors is refused
    dup = json.loads((wf / "journal.jsonl").read_text().splitlines()[1])
    dup["result"] = {"answers": [ANSWERS[0]]}
    with (wf / "journal.jsonl").open("a") as fh:
        fh.write(json.dumps(dup) + "\n")
    with pytest.raises(SystemExit, match="more than once"):
        ae.main(["--journal", str(wf / "journal.jsonl"), "--out", str(out)])


class FakeMonitor:
    """Records the order of calls; the clock is advanced by the test."""

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock

    def score(self, trace: Any) -> Any:
        from types import SimpleNamespace

        self.clock[0] += 2.0
        return SimpleNamespace(score=0.5, details={"digit_mass": 0.9})

    def score_prefixes(self, trace: Any, start: int) -> list[float]:
        self.clock[0] += 7.0
        return [0.1, 0.5]


def test_model_scoring_time_excludes_prefix_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(sm.time, "monotonic", lambda: clock[0])

    class T:
        trace_id = "t"

    rec = sm.model_record(FakeMonitor(clock), T(), 1)
    assert rec["seconds"] == 2.0 and rec["prefix_seconds"] == 7.0
    assert rec["prefix_scores"] == [0.1, 0.5]
    assert "prefix_seconds" not in sm.model_record(FakeMonitor(clock), T(), None)
