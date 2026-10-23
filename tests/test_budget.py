"""Tests for usage caps and the usage ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from baserate import budget, cli

DEFAULT_TOML = """\
cash_usd = 0
claude_agent_runs_per_day = 30
claude_agent_max_turns = 40
claude_single_calls_per_day = 300
claude_review_runs_per_day = 6
local_model_max_params_b = 8
disk_floor_gb = 8
"""


def _defaults(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"cash_usd": 0, **budget.DEFAULT_CAPS}
    data.update(overrides)
    return data


def _write(tmp_path: Path, toml: str, ledger: list[dict[str, Any]]) -> tuple[Path, Path]:
    b = tmp_path / "budget.toml"
    b.write_text(toml)
    led = tmp_path / "ledger.jsonl"
    led.write_text("".join(json.dumps(r) + "\n" for r in ledger))
    return b, led


def test_defaults_parse() -> None:
    parsed = budget.parse_budget(_defaults())
    assert parsed.cash_usd == 0
    assert parsed.caps["claude_agent_runs_per_day"] == 30


def test_repository_budget_file_is_valid() -> None:
    root = Path(__file__).resolve().parents[1]
    parsed = budget.load_budget(root / "budget.toml")
    assert set(parsed.caps) == set(budget.DEFAULT_CAPS)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"cash_usd": 1}, "cash_usd"),
        ({"claude_agent_runs_per_day": 31}, "raised"),
        ({"disk_floor_gb": 4}, "floor"),
        ({"claude_agent_max_turns": -1}, "non-negative"),
        ({"claude_agent_max_turns": True}, "non-negative"),
        ({"claude_agent_max_turns": "5"}, "non-negative"),
        ({"surprise": 1}, "unknown"),
    ],
)
def test_invalid_budgets_are_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(budget.BudgetError, match=message):
        budget.parse_budget(_defaults(**overrides))


def test_missing_cap_is_rejected() -> None:
    data = _defaults()
    del data["claude_review_runs_per_day"]
    with pytest.raises(budget.BudgetError, match="missing"):
        budget.parse_budget(data)


def test_lowering_caps_and_raising_floor_is_allowed() -> None:
    parsed = budget.parse_budget(_defaults(claude_agent_runs_per_day=5, disk_floor_gb=20))
    assert parsed.caps["claude_agent_runs_per_day"] == 5
    assert parsed.caps["disk_floor_gb"] == 20


def test_load_budget_errors_on_bad_file(tmp_path: Path) -> None:
    bad = tmp_path / "budget.toml"
    bad.write_text("cash_usd = = 0")
    with pytest.raises(budget.BudgetError, match="cannot read"):
        budget.load_budget(bad)
    with pytest.raises(budget.BudgetError, match="cannot read"):
        budget.load_budget(tmp_path / "absent.toml")


def test_cap_share_scales_only_claude_caps_and_never_raises() -> None:
    parsed = budget.parse_budget(_defaults())
    assert budget.effective_cap(parsed, "claude_agent_runs_per_day", 0.5) == 15
    assert budget.effective_cap(parsed, "claude_review_runs_per_day", 0.5) == 3
    assert budget.effective_cap(parsed, "claude_agent_runs_per_day", 3.0) == 30
    assert budget.effective_cap(parsed, "local_model_max_params_b", 0.5) == 8


def test_ledger_round_trip_and_clean_report(tmp_path: Path) -> None:
    b, led = _write(
        tmp_path,
        DEFAULT_TOML,
        [
            {
                "ts": "2026-10-23T01:00:00+00:00",
                "kind": "claude_single_call",
                "plan_equivalent_usd": 0.25,
            },
            {"ts": "2026-10-23T02:00:00+00:00", "kind": "claude_agent_run", "turns": 12},
            {"ts": "2026-10-23T03:00:00+00:00", "kind": "local_call", "params_b": 1.5},
        ],
    )
    parsed = budget.load_budget(b)
    report = budget.check_ledger(parsed, budget.read_ledger(led))
    assert report.ok
    assert report.entries == 3
    assert report.plan_equivalent_usd == pytest.approx(0.25)
    lines = budget.format_report(parsed, report)
    assert lines[0] == "cash_usd = 0"
    assert lines[-1] == "caps: all respected"


def test_violations_are_reported() -> None:
    parsed = budget.parse_budget(_defaults(claude_review_runs_per_day=1))
    entries = [
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat("2026-10-23T01:00:00"), kind="claude_review_run"
        ),
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat("2026-10-23T02:00:00"), kind="claude_review_run"
        ),
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat("2026-10-23T03:00:00"),
            kind="claude_agent_run",
            turns=41,
        ),
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat("2026-10-23T04:00:00"), kind="claude_agent_run"
        ),
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat("2026-10-23T05:00:00"), kind="local_call", params_b=70
        ),
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat("2026-10-23T06:00:00"),
            kind="free_tier_call",
            cash_usd=0.01,
        ),
    ]
    report = budget.check_ledger(parsed, entries)
    assert not report.ok
    text = "\n".join(report.violations)
    for needle in [
        "claude_review_run entries > cap 1",
        "41 turns",
        "without turn count",
        "70B",
        "cash_usd 0.01",
    ]:
        assert needle in text
    lines = budget.format_report(parsed, report)
    assert lines[0] == "cash_usd = 0.01"
    assert lines[-1] == "caps: VIOLATED"


def test_smallest_cap_share_of_the_day_applies() -> None:
    parsed = budget.parse_budget(_defaults(claude_review_runs_per_day=4))
    ts = budget.datetime.fromisoformat
    entries = [
        budget.LedgerEntry(ts=ts("2026-10-23T01:00:00"), kind="claude_review_run", cap_share=0.5),
        budget.LedgerEntry(ts=ts("2026-10-23T02:00:00"), kind="claude_review_run"),
        budget.LedgerEntry(ts=ts("2026-10-23T03:00:00"), kind="claude_review_run"),
    ]
    report = budget.check_ledger(parsed, entries)
    assert any("> cap 2" in v for v in report.violations)


def test_calls_remaining() -> None:
    parsed = budget.parse_budget(_defaults(claude_review_runs_per_day=2))
    ts = budget.datetime.fromisoformat
    entries = [budget.LedgerEntry(ts=ts("2026-10-23T01:00:00"), kind="claude_review_run")]
    assert budget.calls_remaining(parsed, entries, "claude_review_run", "2026-10-23") == 1
    assert budget.calls_remaining(parsed, entries, "claude_review_run", "2026-10-24") == 2
    assert budget.calls_remaining(parsed, entries * 5, "claude_review_run", "2026-10-23") == 0
    with pytest.raises(budget.BudgetError):
        budget.calls_remaining(parsed, entries, "local_call", "2026-10-23")


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("not json", "invalid JSON"),
        ("[1, 2]", "not an object"),
        ('{"kind": "local_call"}', "needs ts"),
        ('{"ts": "2026-10-23T00:00:00", "kind": "mystery"}', "unknown ledger kind"),
    ],
)
def test_bad_ledger_lines(tmp_path: Path, line: str, message: str) -> None:
    led = tmp_path / "ledger.jsonl"
    led.write_text(line + "\n\n")
    with pytest.raises(budget.BudgetError, match=message):
        budget.read_ledger(led)


def test_missing_ledger_is_empty(tmp_path: Path) -> None:
    assert budget.read_ledger(tmp_path / "none.jsonl") == []


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b, led = _write(tmp_path, DEFAULT_TOML, [])
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "cash_usd = 0"
    b2, led2 = _write(tmp_path, DEFAULT_TOML.replace("cash_usd = 0", "cash_usd = 5"), [])
    assert budget.main(["--budget", str(b2), "--ledger", str(led2)]) == 1
    assert "caps: VIOLATED" in capsys.readouterr().out
    _, led3 = _write(
        tmp_path, DEFAULT_TOML, [{"ts": "2026-10-23T00:00:00", "kind": "local_call", "cash_usd": 1}]
    )
    assert budget.main(["--budget", str(b), "--ledger", str(led3)]) == 1


def test_cli_dispatches_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b, led = _write(tmp_path, DEFAULT_TOML, [])
    assert cli.main(["budget", "--budget", str(b), "--ledger", str(led)]) == 0
    assert "caps: all respected" in capsys.readouterr().out


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "baserate" in capsys.readouterr().out
