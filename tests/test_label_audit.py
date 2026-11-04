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


def evidence(*tools: list[str]) -> dict[str, Any]:
    return {
        "workflow_run": "wf_x",
        "agents": [
            {"label": f"audit:{i}", "tools_used": t, "transcript_sha256": f"{i:064x}"}
            for i, t in enumerate(tools)
        ],
    }


def test_evidence_refuses_any_tool_beyond_returning_answers() -> None:
    ok = la.check_evidence(evidence(["StructuredOutput"], []))
    assert ok["agents"] == 2 and ok["tools_used"] == ["StructuredOutput"]
    with pytest.raises(SystemExit, match="beyond returning answers"):
        la.check_evidence(evidence(["StructuredOutput"], ["Read", "StructuredOutput"]))
    with pytest.raises(SystemExit, match="no auditor agents"):
        la.check_evidence({"agents": []})


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
    la.score("claude subagent (test)", evidence(["StructuredOutput"]))
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
        (wf / f"agent-{agent}.jsonl").write_text(f'{{"n": {i}}}\n')
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
    assert la.check_evidence(ev)["tools_used"] == ["StructuredOutput"]


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
