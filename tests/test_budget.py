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
                "cap_share": 1.0,
            },
            {
                "ts": "2026-10-23T02:00:00+00:00",
                "kind": "claude_agent_run",
                "turns": 12,
                "cap_share": 1.0,
            },
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


# ---------------------------------------------------------------- waivers (D26)

D26 = """\
[[waiver]]
decision = "D26"
day = "2026-11-11"
kind = "claude_review_run"
max_entries = 2
approved_by = "Raj Kashikar"
approved_on = "2026-11-11"
"""


D26_RECORD = (
    "Waiver record: decision=D26; day=2026-11-11; kind=claude_review_run; max_entries=2; "
    "approved_by=Raj Kashikar; approved_on=2026-11-11"
)
D26_APPROVAL = (
    "# Decisions\n\n## D26 (2026-11-11) Waiver: the 2026-11-11 claude_review_run overrun\n\n"
    f"Approved by Raj on 2026-11-11.\n\n{D26_RECORD}\n\n## D27 (2026-11-11) Other\n\nText.\n"
)


def _runs(kind: str, day: str, n: int, share: float = 0.2) -> list[budget.LedgerEntry]:
    return [
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat(f"{day}T0{i % 10}:00:00+00:00"),
            kind=kind,
            cap_share=share,
        )
        for i in range(n)
    ]


def _waivers(text: str = D26) -> list[budget.Waiver]:
    return budget.parse_waivers(budget.tomllib.loads(text))


def test_waiver_excuses_exactly_its_overrun() -> None:
    caps = budget.parse_budget(_defaults())
    entries = _runs("claude_review_run", "2026-11-11", 2)
    assert not budget.check_ledger(caps, entries).ok  # one review run a day at share 0.2
    report = budget.check_ledger(caps, entries, _waivers())
    assert report.ok and report.violations == []
    assert report.waived == [
        "2026-11-11: 2 claude_review_run entries > cap 1, waived by D26 "
        "(approved by Raj Kashikar on 2026-11-11)"
    ]
    lines = budget.format_report(caps, report)
    assert lines[-1] == "caps: respected except waived overruns (D26)"
    assert "caps: all respected" not in lines


@pytest.mark.parametrize(
    ("entries", "needle"),
    [
        (_runs("claude_review_run", "2026-11-11", 3), "beyond waiver D26"),
        (
            _runs("claude_review_run", "2026-11-11", 2)
            + _runs("claude_review_run", "2026-11-12", 2),
            "2026-11-12: 2 claude_review_run entries > cap 1",
        ),
        (
            _runs("claude_review_run", "2026-11-11", 2)
            + _runs("claude_single_call", "2026-11-11", 61),
            "2026-11-11: 61 claude_single_call entries > cap 60",
        ),
    ],
    ids=["third-run-that-day", "another-day", "another-kind"],
)
def test_any_other_overrun_still_fails(entries: list[budget.LedgerEntry], needle: str) -> None:
    caps = budget.parse_budget(_defaults())
    report = budget.check_ledger(caps, entries, _waivers())
    assert not report.ok
    assert any(needle in v for v in report.violations), report.violations


def test_a_waiver_that_excuses_nothing_fails() -> None:
    caps = budget.parse_budget(_defaults())
    report = budget.check_ledger(caps, _runs("claude_review_run", "2026-11-11", 1), _waivers())
    assert not report.ok
    assert any("waiver D26 excuses no overrun" in v for v in report.violations)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (D26.replace('approved_on = "2026-11-11"', 'approved_on = "2026-11-10"'), "before"),
        (D26.replace("claude_review_run", "local_call"), "no daily cap"),
        (D26.replace("max_entries = 2", "max_entries = 0"), "max_entries"),
        (D26.replace("max_entries = 2", "max_entries = true"), "max_entries"),
        (D26 + 'note = "x"\n', "unknown"),
        (D26.replace('decision = "D26"\n', ""), "missing"),
        (D26.replace('day = "2026-11-11"', 'day = "2026-13-11"'), "date"),
        (D26 + "\n" + D26, "repeated decision|duplicate"),
        (D26 + "\n" + D26.replace('"D26"', '"D27"'), "duplicate waiver for 2026-11-11"),
        ("extra = 1\n" + D26, "unknown"),
    ],
    ids=[
        "preauthorized",
        "uncapped-kind",
        "zero-entries",
        "bool-entries",
        "extra-key",
        "missing-key",
        "bad-date",
        "duplicate",
        "same-day-and-kind",
        "extra-table",
    ],
)
def test_invalid_waivers_are_refused(text: str, message: str) -> None:
    with pytest.raises(budget.BudgetError, match=message):
        _waivers(text)


def test_repository_waivers_match_the_repository_ledger() -> None:
    root = Path(__file__).resolve().parents[1]
    caps = budget.load_budget(root / "budget.toml")
    waivers = budget.load_waivers(root / "budget_waivers.toml")
    assert [(w.decision, w.day, w.kind) for w in waivers] == [
        ("D26", "2026-11-11", "claude_review_run")
    ]
    report = budget.check_ledger(
        caps, budget.read_ledger(root / "artifacts" / "usage" / "ledger.jsonl"), waivers
    )
    assert report.ok and len(report.waived) == 1 and report.waived[0].startswith("2026-11-11:")
    decisions = (root / "DECISIONS.md").read_text(encoding="utf-8")
    assert budget.waiver_record_problems(waivers, decisions) == []


def test_cli_reports_the_waiver(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b, led = _write(
        tmp_path,
        DEFAULT_TOML,
        [
            {"ts": f"2026-11-11T0{i}:00:00+00:00", "kind": "claude_review_run", "cap_share": 0.2}
            for i in range(2)
        ],
    )
    w = tmp_path / "budget_waivers.toml"
    w.write_text(D26)
    (tmp_path / "DECISIONS.md").write_text(D26_APPROVAL)
    assert budget.main(["--budget", str(b), "--ledger", str(led), "--waivers", str(w)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[-1] == "caps: respected except waived overruns (D26)"
    assert any(line.startswith("WAIVED 2026-11-11: 2 claude_review_run") for line in out)
    w.unlink()  # without the waiver the same ledger fails
    assert budget.main(["--budget", str(b), "--ledger", str(led), "--waivers", str(w)]) == 1


def test_days_are_utc_days_whatever_the_offset() -> None:
    """A run stamped in another offset counts toward its UTC day's cap."""
    caps = budget.parse_budget(_defaults())
    entries = [
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat("2026-11-13T20:00:00+00:00"),
            kind="claude_review_run",
            cap_share=0.2,
        ),
        budget.LedgerEntry(  # 2026-11-13T20:30Z, written with a +05:00 offset
            ts=budget.datetime.fromisoformat("2026-11-14T01:30:00+05:00"),
            kind="claude_review_run",
            cap_share=0.2,
        ),
    ]
    report = budget.check_ledger(caps, entries)
    assert report.daily_counts == {("2026-11-13", "claude_review_run"): 2}
    assert any("2026-11-13: 2 claude_review_run entries > cap 1" in v for v in report.violations)
    assert budget.calls_remaining(caps, entries, "claude_review_run", "2026-11-13", 0.2) == 0


# ------------------------------------------------- exact approval records (QA)


def test_decision_ids_must_be_unique_and_plain() -> None:
    second = D26.replace("2026-11-11", "2026-11-09").replace(
        "claude_review_run", "claude_single_call"
    )
    with pytest.raises(budget.BudgetError, match="repeated decision"):
        _waivers(D26 + "\n" + second)
    with pytest.raises(budget.BudgetError, match="decision id"):
        _waivers(D26.replace('decision = "D26"', 'decision = "D26, D26"'))


def test_the_approval_record_pins_every_field() -> None:
    waivers = _waivers()
    assert budget.waiver_record_line(waivers[0]) == D26_RECORD
    assert budget.waiver_record_problems(waivers, D26_APPROVAL) == []
    raised = _waivers(D26.replace("max_entries = 2", "max_entries = 6"))
    assert budget.waiver_record_problems(raised, D26_APPROVAL)  # the record says 2


@pytest.mark.parametrize(
    "decisions",
    [
        D26_APPROVAL.replace(D26_RECORD, "This waiver is NOT approved by Raj on 2026-11-11."),
        D26_APPROVAL.replace(D26_RECORD, "> " + D26_RECORD),
        D26_APPROVAL.replace(D26_RECORD, "").replace("Text.", D26_RECORD),
        D26_APPROVAL.replace(D26_RECORD, D26_RECORD.replace("max_entries=2", "max_entries=3")),
        D26_APPROVAL + "\n## D26 (2026-11-12) A second D26\n\nText.\n",
        D26_APPROVAL.replace("Approved by Raj on", "This waiver is not approved by Raj on"),
        "# Decisions\n",
    ],
    ids=[
        "not-approved",
        "quoted",
        "other-section",
        "other-count",
        "two-sections",
        "record-kept-but-not-approved",
        "absent",
    ],
)
def test_a_waiver_without_its_exact_record_fails(decisions: str) -> None:
    assert budget.waiver_record_problems(_waivers(), decisions)


def test_only_raj_approves_waivers() -> None:
    other = _waivers(D26.replace("Raj Kashikar", "Someone Else"))
    record = D26_APPROVAL.replace("approved_by=Raj Kashikar", "approved_by=Someone Else")
    assert budget.waiver_record_problems(other, record)


def test_cli_refuses_a_stretched_waiver(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runs = [
        {"ts": f"2026-11-11T0{i}:00:00+00:00", "kind": "claude_review_run", "cap_share": 0.2}
        for i in range(6)
    ]
    b, led = _write(tmp_path, DEFAULT_TOML, runs)
    (tmp_path / "budget_waivers.toml").write_text(D26.replace("max_entries = 2", "max_entries = 6"))
    (tmp_path / "DECISIONS.md").write_text(D26_APPROVAL)
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "caps: VIOLATED"


def test_cli_refuses_a_waiver_without_decisions(tmp_path: Path) -> None:
    runs = [
        {"ts": f"2026-11-11T0{i}:00:00+00:00", "kind": "claude_review_run", "cap_share": 0.2}
        for i in range(2)
    ]
    b, led = _write(tmp_path, DEFAULT_TOML, runs)
    (tmp_path / "budget_waivers.toml").write_text(D26)
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 1


# ------------------------------------------------------------- cap_share (QA)


@pytest.mark.parametrize("kind", ["claude_review_run", "claude_single_call", "claude_agent_run"])
def test_claude_entries_must_record_their_cap_share(kind: str) -> None:
    with pytest.raises(budget.BudgetError, match="cap_share"):
        budget.parse_entry({"ts": "2026-11-12T01:00:00+00:00", "kind": kind, "turns": 1})


def test_local_calls_may_omit_cap_share() -> None:
    entry = budget.parse_entry({"ts": "2026-10-23T02:00:00", "kind": "local_call"})
    assert entry.cap_share == 1.0


@pytest.mark.parametrize("share", ["nan", 0, -0.2, 1.5, True, "x"])
def test_invalid_cap_shares_are_errors(share: Any) -> None:
    value = float("nan") if share == "nan" else share
    with pytest.raises(budget.BudgetError, match="cap_share"):
        budget.parse_entry({"ts": "2026-10-23T02:00:00", "kind": "local_call", "cap_share": value})


def test_cap_share_in_the_environment_lowers_a_day_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = [
        {"ts": f"2026-11-13T0{i}:00:00+00:00", "kind": "claude_review_run", "cap_share": 1.0}
        for i in range(2)
    ]
    b, led = _write(tmp_path, DEFAULT_TOML, runs)
    monkeypatch.delenv("CAP_SHARE", raising=False)
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 0  # cap 6 at share 1
    monkeypatch.setenv("CAP_SHARE", "0.2")
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 1  # cap 1 at share 0.2
    assert "2026-11-13: 2 claude_review_run entries > cap 1" in capsys.readouterr().out
    monkeypatch.setenv("CAP_SHARE", "2")
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 1  # an invalid share


def test_bad_inputs_give_a_clean_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    b, led = _write(tmp_path, DEFAULT_TOML, [])
    bad = tmp_path / "budget_waivers.toml"
    bad.write_bytes(b"# caf\xe9\n")
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("budget error:") and out[-1] == "caps: VIOLATED"
    bad.unlink()
    _, led2 = _write(
        tmp_path, DEFAULT_TOML, [{"ts": "0001-01-01T00:30:00+01:00", "kind": "local_call"}]
    )
    assert budget.main(["--budget", str(b), "--ledger", str(led2)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("budget error:") and out[-1] == "caps: VIOLATED"


# ---------------------------------------------------------------- cap share in budget.toml (D29)


def test_budget_records_the_cap_share_and_it_lowers_caps_without_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CAP_SHARE", raising=False)
    caps = budget.parse_budget(_defaults(cap_share=0.2))
    assert caps.cap_share == 0.2
    share = budget.share_in_force(caps, None)
    assert share == 0.2
    entries = _runs("claude_review_run", "2026-11-20", 2, share=1.0)  # entries claim full share
    report = budget.check_ledger(caps, entries, (), share)
    assert any("2026-11-20: 2 claude_review_run entries > cap 1" in v for v in report.violations)
    assert budget.calls_remaining(caps, entries[:1], "claude_review_run", "2026-11-20", share) == 0


def test_the_environment_can_only_lower_the_recorded_share() -> None:
    caps = budget.parse_budget(_defaults(cap_share=0.2))
    assert budget.share_in_force(caps, "0.5") == 0.2
    assert budget.share_in_force(caps, "0.1") == 0.1
    assert budget.share_in_force(budget.parse_budget(_defaults()), None) == 1.0
    with pytest.raises(budget.BudgetError, match="cap_share"):
        budget.share_in_force(caps, "2")


@pytest.mark.parametrize("bad", [0, 1.5, -0.2, True, "0.2", float("nan")])
def test_invalid_recorded_shares_are_refused(bad: Any) -> None:
    with pytest.raises(budget.BudgetError, match="cap_share"):
        budget.parse_budget(_defaults(cap_share=bad))


def test_repository_budget_records_the_project_share() -> None:
    root = Path(__file__).resolve().parents[1]
    assert budget.load_budget(root / "budget.toml").cap_share == 0.2


# ---------------------------------------------------------------- banked reviews vs the ledger (B6)


def _review(at: str, commit: str = "a" * 40) -> budget.BankedReview:
    at_ts = budget.datetime.fromisoformat(at)
    return budget.BankedReview(reviewed_commit=commit, reviewed_at=at_ts)


def _review_runs(*ts: str) -> list[budget.LedgerEntry]:
    return [
        budget.LedgerEntry(
            ts=budget.datetime.fromisoformat(t), kind="claude_review_run", cap_share=0.2
        )
        for t in ts
    ]


def test_each_banked_review_needs_its_own_logged_run() -> None:
    entries = _review_runs("2026-11-12T00:06:05+00:00")
    assert budget.unlogged_reviews([_review("2026-11-12T00:29:00+00:00")], entries) == []
    two = [
        _review("2026-11-12T01:00:00+00:00", "b" * 40),
        _review("2026-11-12T03:00:00+00:00", "c" * 40),
    ]
    problems = budget.unlogged_reviews(two, entries)
    assert len(problems) == 1 and "c" * 12 in problems[0]


@pytest.mark.parametrize(
    ("run", "review", "ok"),
    [
        ("2026-11-12T00:30:00+00:00", "2026-11-12T00:29:00+00:00", False),
        ("2026-11-11T23:55:00+00:00", "2026-11-12T00:20:00+00:00", True),
        ("2026-11-10T23:55:00+00:00", "2026-11-12T00:20:00+00:00", False),
    ],
    ids=["logged-after-the-review", "started-the-previous-utc-day", "two-days-earlier"],
)
def test_a_review_run_is_logged_at_or_before_it(run: str, review: str, ok: bool) -> None:
    problems = budget.unlogged_reviews([_review(review)], _review_runs(run))
    assert (problems == []) is ok


def _bank(review_dir: Path, latest: tuple[str, str], *history: tuple[str, str]) -> None:
    (review_dir / "history").mkdir(parents=True, exist_ok=True)
    (review_dir / "latest.json").write_text(
        json.dumps({"reviewed_commit": latest[0], "reviewed_at": latest[1]})
    )
    for i, (commit, at) in enumerate(history):
        (review_dir / "history" / f"h{i}.json").write_text(
            json.dumps({"reviewed_commit": commit, "reviewed_at": at})
        )


def test_read_banked_reviews_takes_latest_and_history(tmp_path: Path) -> None:
    d = tmp_path / "artifacts" / "review"
    _bank(d, ("b" * 40, "2026-11-12T00:29:00Z"), ("a" * 40, "2026-11-11T05:12:30Z"))
    got = budget.read_banked_reviews(d)
    assert [(r.reviewed_commit[0], r.reviewed_at.isoformat()) for r in got] == [
        ("a", "2026-11-11T05:12:30+00:00"),
        ("b", "2026-11-12T00:29:00+00:00"),
    ]


def test_cli_fails_on_an_unlogged_banked_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    b, led = _write(
        tmp_path,
        DEFAULT_TOML + "cap_share = 0.2\n",
        [{"ts": "2026-11-12T00:06:05+00:00", "kind": "claude_review_run", "cap_share": 0.2}],
    )
    d = tmp_path / "artifacts" / "review"
    _bank(d, ("c" * 40, "2026-11-12T03:00:00Z"), ("b" * 40, "2026-11-12T01:00:00Z"))
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 1
    out = capsys.readouterr().out
    assert "has no claude_review_run entry" in out and out.rstrip().endswith("caps: VIOLATED")
    (d / "history" / "h0.json").unlink()  # one review, one logged run
    assert budget.main(["--budget", str(b), "--ledger", str(led)]) == 0


def test_repository_reviews_are_all_logged() -> None:
    root = Path(__file__).resolve().parents[1]
    reviews = budget.read_banked_reviews(root / "artifacts" / "review")
    entries = budget.read_ledger(root / "artifacts" / "usage" / "ledger.jsonl")
    assert len(reviews) >= 3 and budget.unlogged_reviews(reviews, entries) == []
