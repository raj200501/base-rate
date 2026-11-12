"""Tests for the daily review cap on the reviewer-subagent path (tools/review.py).

Brief section 9 rule 5 asks for caps enforced in code. In this environment the
review runs as the ``reviewer`` subagent (DECISIONS.md D2), so ``make review-start``
checks the current UTC day's review cap at CAP_SHARE and logs the run before the
subagent starts (D9, D27); a day whose cap is used up gets no review.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"

BUDGET = """\
cash_usd = 0
claude_agent_runs_per_day = 30
claude_agent_max_turns = 40
claude_single_calls_per_day = 300
claude_review_runs_per_day = 6
local_model_max_params_b = 8
disk_floor_gb = 8
"""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_under_test", TOOLS / "review.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["review_under_test"] = module
    spec.loader.exec_module(module)
    return module


review = _load()


def _root(tmp_path: Path, entries: list[dict[str, object]] | None = None) -> Path:
    root = tmp_path / "repo"
    (root / "artifacts" / "usage").mkdir(parents=True)
    (root / "budget.toml").write_text(BUDGET)
    ledger = root / "artifacts" / "usage" / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(e) + "\n" for e in entries or []))
    return root


def _ledger(root: Path) -> list[dict[str, object]]:
    text = (root / "artifacts" / "usage" / "ledger.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _run(ts: str) -> dict[str, object]:
    return {"ts": ts, "kind": "claude_review_run", "cap_share": 0.2, "cash_usd": 0}


NOW = datetime(2026, 11, 12, 0, 5, tzinfo=UTC)


def test_a_used_up_day_gets_no_review_and_logs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAP_SHARE", "0.2")
    root = _root(tmp_path, [_run("2026-11-12T00:01:00+00:00")])
    before = _ledger(root)
    assert review.start_subagent(root, now=NOW) != 0
    out = capsys.readouterr().out
    assert "daily review cap used up" in out and "no review runs today" in out
    assert "1 claude_review_run" in out and "cap 1" in out and "CAP_SHARE=0.2" in out
    assert "run the `reviewer` subagent now" not in out
    assert _ledger(root) == before


def test_under_the_cap_one_entry_is_logged_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAP_SHARE", "0.2")
    root = _root(tmp_path, [_run("2026-11-11T05:25:05+00:00")])  # yesterday's run
    assert review.start_subagent(root, now=NOW) == 0
    assert "run the `reviewer` subagent now" in capsys.readouterr().out
    entries = _ledger(root)
    assert len(entries) == 2
    new = entries[-1]
    assert new["kind"] == "claude_review_run" and new["backend"] == "claude-subagent"
    assert new["purpose"] == "make review" and new["cash_usd"] == 0
    assert new["plan_equivalent_usd"] == 0 and new["cap_share"] == 0.2
    assert str(new["ts"]).endswith("+00:00")
    assert datetime.fromisoformat(str(new["ts"])) == NOW
    assert "logged when the run starts" in str(new["note"])


def test_a_second_start_the_same_day_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAP_SHARE", "0.2")  # one review run a day (D9)
    root = _root(tmp_path)
    assert review.start_subagent(root, now=NOW) == 0
    assert review.start_subagent(root, now=NOW.replace(hour=23, minute=59)) != 0
    assert len(_ledger(root)) == 1


def test_the_day_boundary_is_utc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAP_SHARE", "0.2")
    # 2026-11-12T23:30-02:00 is 2026-11-13T01:30Z, so it belongs to 2026-11-13
    root = _root(tmp_path, [_run("2026-11-12T23:30:00-02:00")])
    late_on_the_12th = datetime(2026, 11, 12, 23, 59, tzinfo=UTC)
    assert review.start_subagent(root, now=late_on_the_12th) == 0  # the 12th had none
    early_on_the_13th = datetime(2026, 11, 13, 0, 10, tzinfo=UTC)
    assert review.start_subagent(root, now=early_on_the_13th) != 0  # the 13th has one


@pytest.mark.parametrize("share", ["0", "-0.2", "1.5", "nan", "abc"])
def test_an_invalid_cap_share_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, share: str
) -> None:
    monkeypatch.setenv("CAP_SHARE", share)
    root = _root(tmp_path)
    assert review.start_subagent(root, now=NOW) != 0
    assert _ledger(root) == []


def test_without_cap_share_the_full_cap_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAP_SHARE", raising=False)
    root = _root(tmp_path)
    assert review.start_subagent(root, now=NOW) == 0
    assert _ledger(root)[-1]["cap_share"] == 1.0


def test_plain_review_points_to_review_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(review, "headless_blockers", lambda root: ["claude CLI not found"])
    root = _root(tmp_path)
    assert review.main(["--root", str(root)]) != 0  # no review file yet
    out = capsys.readouterr().out
    assert "make review-start" in out
    assert _ledger(root) == []  # plain make review logs nothing


def test_main_dispatches_start_subagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CAP_SHARE", "0.2")
    root = _root(tmp_path)
    assert review.main(["--start-subagent", "--root", str(root)]) == 0
    assert len(_ledger(root)) == 1
    assert review.main(["--start-subagent", "--root", str(root)]) != 0  # same UTC day
    assert len(_ledger(root)) == 1


def test_headless_command_carries_the_turn_cap() -> None:
    cmd = review.headless_command("system prompt", "review this", max_turns=8)
    assert cmd[:2] == ["claude", "-p"]
    i = cmd.index("--max-turns")
    assert cmd[i + 1] == "8"
    assert cmd[-1] == "review this"


def test_headless_turn_cap_is_scaled_by_cap_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    monkeypatch.setenv("CAP_SHARE", "0.2")
    assert review.headless_turn_cap(root) == 8  # 40 turns at a share of 0.2
    monkeypatch.delenv("CAP_SHARE", raising=False)
    assert review.headless_turn_cap(root) == 40


def test_headless_ledger_entry_records_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAP_SHARE", "0.2")
    root = _root(tmp_path)
    review._append_ledger(root, 0.5, turns=7)
    entry = _ledger(root)[-1]
    assert entry["kind"] == "claude_review_run" and entry["turns"] == 7
    assert entry["cap_share"] == 0.2


def test_the_budget_files_share_binds_without_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """budget.toml records the project's share (D29), so an unset CAP_SHARE cannot raise caps."""
    monkeypatch.delenv("CAP_SHARE", raising=False)
    root = _root(tmp_path)
    (root / "budget.toml").write_text(BUDGET + "cap_share = 0.2\n")
    assert review.start_subagent(root, now=NOW) == 0
    assert _ledger(root)[0]["cap_share"] == 0.2
    assert review.start_subagent(root, now=NOW.replace(hour=12)) != 0  # one a day at 0.2
    assert len(_ledger(root)) == 1
    monkeypatch.setenv("CAP_SHARE", "1")  # the environment cannot raise it
    assert review.start_subagent(root, now=NOW.replace(hour=13)) != 0


def test_review_start_refuses_when_a_banked_review_is_unlogged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review-start runs the same checks as make budget (review N24), banked reviews included."""
    monkeypatch.delenv("CAP_SHARE", raising=False)
    root = _root(tmp_path)
    (root / "budget.toml").write_text(BUDGET + "cap_share = 0.2\n")
    review_dir = root / "artifacts" / "review"
    review_dir.mkdir(parents=True)
    (review_dir / "latest.json").write_text(
        json.dumps({"reviewed_commit": "a" * 40, "reviewed_at": "2026-11-11T05:00:00Z"})
    )
    assert review.start_subagent(root, now=NOW) != 0  # the 2026-11-11 review was never logged
    assert _ledger(root) == []


def test_review_start_refuses_an_unapproved_waiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CAP_SHARE", raising=False)
    root = _root(
        tmp_path,
        [_run("2026-11-11T01:00:00+00:00"), _run("2026-11-11T02:00:00+00:00")],
    )
    (root / "budget.toml").write_text(BUDGET + "cap_share = 0.2\n")
    (root / "budget_waivers.toml").write_text(
        '[[waiver]]\ndecision = "D26"\nday = "2026-11-11"\nkind = "claude_review_run"\n'
        'max_entries = 2\napproved_by = "Raj Kashikar"\napproved_on = "2026-11-11"\n'
    )
    (root / "DECISIONS.md").write_text("# Decisions\n")  # no approval record
    assert review.start_subagent(root, now=NOW) != 0
    assert len(_ledger(root)) == 2
