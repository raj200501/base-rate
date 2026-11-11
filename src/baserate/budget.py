"""Usage caps and the usage ledger (brief section 8).

``budget.toml`` holds the caps. They may be lowered and never raised, and
``cash_usd`` is always zero. Every model call is logged to
``artifacts/usage/ledger.jsonl``; :func:`check_ledger` verifies that no cap
was exceeded and that no cash was spent. ``plan_equivalent_usd`` (the API-price
estimate that headless Claude reports) is tracked separately and never counts
as cash.

``budget_waivers.toml`` lists past overruns that Raj accepted after the fact,
each recorded in DECISIONS.md. A waiver never changes a cap: it excuses exactly
one overrun (one ledger kind on one UTC day, up to a stated number of
entries), it may not be dated before that overrun, and a waiver that excuses
nothing is itself an error. Every other overrun still fails.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

DEFAULT_CAPS: Mapping[str, float] = {
    "claude_agent_runs_per_day": 30,
    "claude_agent_max_turns": 40,
    "claude_single_calls_per_day": 300,
    "claude_review_runs_per_day": 6,
    "local_model_max_params_b": 8,
    "disk_floor_gb": 8,
}
"""Defaults from the brief. ``disk_floor_gb`` is a floor, so raising it is
the safe direction; every other cap may only go down."""

FLOOR_KEYS: frozenset[str] = frozenset({"disk_floor_gb"})

DAILY_CAP_FOR_KIND: Mapping[str, str] = {
    "claude_agent_run": "claude_agent_runs_per_day",
    "claude_single_call": "claude_single_calls_per_day",
    "claude_review_run": "claude_review_runs_per_day",
}
"""Ledger entry kinds that count against a per-day cap."""

KNOWN_KINDS: frozenset[str] = frozenset(DAILY_CAP_FOR_KIND) | {"local_call", "free_tier_call"}


class BudgetError(ValueError):
    """Raised when ``budget.toml`` or the ledger is malformed."""


@dataclass(frozen=True)
class Budget:
    """Validated caps from ``budget.toml``."""

    cash_usd: float
    caps: Mapping[str, float]


@dataclass(frozen=True)
class LedgerEntry:
    """One logged model call or run."""

    ts: datetime
    kind: str
    cash_usd: float = 0.0
    plan_equivalent_usd: float = 0.0
    turns: int | None = None
    params_b: float | None = None
    cap_share: float = 1.0


@dataclass
class BudgetReport:
    """Result of checking the ledger against the caps."""

    cash_usd: float
    plan_equivalent_usd: float
    entries: int
    daily_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    waived: list[str] = field(default_factory=list)
    waived_by: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no cash was spent and every cap was respected."""
        return self.cash_usd == 0 and not self.violations


def parse_budget(data: Mapping[str, Any]) -> Budget:
    """Validate parsed ``budget.toml`` content.

    Args:
        data: The parsed TOML table.

    Returns:
        The validated budget.

    Raises:
        BudgetError: If cash is not zero, a cap is missing or unknown, a value
            is not a non-negative number, or a cap was raised above its default.
    """
    if data.get("cash_usd") != 0:
        raise BudgetError("cash_usd must be exactly 0")
    unknown = sorted(set(data) - set(DEFAULT_CAPS) - {"cash_usd"})
    if unknown:
        raise BudgetError(f"unknown keys in budget.toml: {', '.join(unknown)}")
    caps: dict[str, float] = {}
    for key, default in DEFAULT_CAPS.items():
        if key not in data:
            raise BudgetError(f"missing cap {key}")
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise BudgetError(f"cap {key} must be a non-negative number")
        if key in FLOOR_KEYS:
            if value < default:
                raise BudgetError(f"floor {key} lowered below default {default}")
        elif value > default:
            raise BudgetError(f"cap {key} raised above default {default}")
        caps[key] = float(value)
    return Budget(cash_usd=0.0, caps=caps)


def load_budget(path: Path) -> Budget:
    """Read and validate ``budget.toml``.

    Args:
        path: Location of the TOML file.

    Returns:
        The validated budget.

    Raises:
        BudgetError: If the file is unreadable, not TOML, or invalid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BudgetError(f"cannot read {path}: {exc}") from exc
    return parse_budget(data)


def effective_cap(budget: Budget, key: str, cap_share: float = 1.0) -> float:
    """Return a cap after applying ``CAP_SHARE``.

    Only ``claude_*`` caps scale. A share above 1 is treated as 1, so sharing
    can never raise a cap.

    Args:
        budget: The validated budget.
        key: Cap name.
        cap_share: The ``CAP_SHARE`` value in effect.

    Returns:
        The cap, floored to a whole number for per-day and per-run counts.
    """
    value = budget.caps[key]
    if key.startswith("claude_"):
        share = min(max(cap_share, 0.0), 1.0)
        return float(math.floor(value * share))
    return value


def parse_entry(record: Mapping[str, Any]) -> LedgerEntry:
    """Validate one ledger record.

    Args:
        record: A decoded JSON object from ``ledger.jsonl``.

    Returns:
        The parsed entry.

    Raises:
        BudgetError: If required fields are missing or have the wrong type.
    """
    try:
        ts = datetime.fromisoformat(str(record["ts"]))
        kind = str(record["kind"])
    except (KeyError, ValueError) as exc:
        raise BudgetError(f"ledger record needs ts and kind: {exc}") from exc
    if kind not in KNOWN_KINDS:
        raise BudgetError(f"unknown ledger kind {kind!r}")
    turns = record.get("turns")
    params_b = record.get("params_b")
    return LedgerEntry(
        ts=ts,
        kind=kind,
        cash_usd=float(record.get("cash_usd", 0.0)),
        plan_equivalent_usd=float(record.get("plan_equivalent_usd", 0.0)),
        turns=None if turns is None else int(turns),
        params_b=None if params_b is None else float(params_b),
        cap_share=float(record.get("cap_share", 1.0)),
    )


def read_ledger(path: Path) -> list[LedgerEntry]:
    """Read ``ledger.jsonl``; a missing file is an empty ledger.

    Args:
        path: Ledger location.

    Returns:
        Parsed entries in file order.

    Raises:
        BudgetError: If a line is not valid JSON or not a valid record.
    """
    if not path.exists():
        return []
    entries = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise BudgetError(f"{path}:{lineno}: invalid JSON") from exc
        if not isinstance(record, dict):
            raise BudgetError(f"{path}:{lineno}: record is not an object")
        entries.append(parse_entry(record))
    return entries


@dataclass(frozen=True)
class Waiver:
    """One past cap overrun that Raj accepted (recorded in DECISIONS.md).

    Attributes:
        decision: The DECISIONS.md entry that records the approval (``D26``).
        day: The UTC day of the overrun (ISO date).
        kind: The ledger kind whose per-day cap was exceeded.
        max_entries: The most entries of ``kind`` on ``day`` the waiver excuses.
        approved_by: Who approved it.
        approved_on: When (ISO date, on or after ``day``).
    """

    decision: str
    day: str
    kind: str
    max_entries: int
    approved_by: str
    approved_on: str


WAIVER_KEYS: frozenset[str] = frozenset(
    {"decision", "day", "kind", "max_entries", "approved_by", "approved_on"}
)


def _iso_day(value: Any, what: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise BudgetError(f"{what} is not an ISO date: {value!r}") from exc


def parse_waivers(data: Mapping[str, Any]) -> list[Waiver]:
    """Validate the ``[[waiver]]`` tables of ``budget_waivers.toml``.

    Raises:
        BudgetError: On an unknown or missing field, a kind without a daily cap,
            a non-positive entry count, an invalid date, a waiver dated before its
            overrun (waivers accept past overruns only), or a repeated day and kind.
    """
    unknown = sorted(set(data) - {"waiver"})
    if unknown:
        raise BudgetError(f"unknown keys in budget_waivers.toml: {unknown}")
    raw = data.get("waiver", [])
    if not isinstance(raw, list):
        raise BudgetError("waivers must be [[waiver]] tables")
    out: list[Waiver] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise BudgetError("each waiver must be a table")
        extra = sorted(set(item) - WAIVER_KEYS)
        if extra:
            raise BudgetError(f"unknown waiver keys: {extra}")
        missing = sorted(WAIVER_KEYS - set(item))
        if missing:
            raise BudgetError(f"waiver missing keys: {missing}")
        decision = str(item["decision"])
        day = _iso_day(item["day"], f"waiver {decision} day")
        approved_on = _iso_day(item["approved_on"], f"waiver {decision} approved_on")
        if approved_on < day:
            raise BudgetError(
                f"waiver {decision} is approved before its overrun day {day}; "
                "a waiver may only accept a past overrun"
            )
        kind = str(item["kind"])
        if kind not in DAILY_CAP_FOR_KIND:
            raise BudgetError(f"waiver {decision} names kind {kind!r}, which has no daily cap")
        n = item["max_entries"]
        if isinstance(n, bool) or not isinstance(n, int) or n < 1:
            raise BudgetError(f"waiver {decision} max_entries must be a positive integer")
        who = str(item["approved_by"]).strip()
        if not who:
            raise BudgetError(f"waiver {decision} needs approved_by")
        key = (day.isoformat(), kind)
        if key in seen:
            raise BudgetError(f"duplicate waiver for {key[0]} {kind}")
        seen.add(key)
        out.append(Waiver(decision, day.isoformat(), kind, n, who, approved_on.isoformat()))
    return out


def load_waivers(path: Path) -> list[Waiver]:
    """Read ``budget_waivers.toml``; a missing file means no waivers."""
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise BudgetError(f"cannot read {path}: {exc}") from exc
    return parse_waivers(data)


def check_ledger(
    budget: Budget, entries: Iterable[LedgerEntry], waivers: Sequence[Waiver] = ()
) -> BudgetReport:
    """Check logged usage against the caps.

    A day's cap is the cap scaled by the smallest ``cap_share`` recorded that
    day, so a lowered share is never escaped by mixing entries. A per-day
    overrun is excused only by a waiver for that exact day and kind, and only up
    to the waiver's ``max_entries``; a waiver that excuses no overrun is a
    violation, so a waiver cannot sit unused or be stretched.

    Args:
        budget: The validated budget.
        entries: Ledger entries.
        waivers: Accepted past overruns (``budget_waivers.toml``).

    Returns:
        Totals, per-day counts, every violation found, and every waived overrun.
    """
    items = list(entries)
    report = BudgetReport(
        cash_usd=sum(e.cash_usd for e in items),
        plan_equivalent_usd=sum(e.plan_equivalent_usd for e in items),
        entries=len(items),
    )
    counts: Counter[tuple[str, str]] = Counter()
    min_share: dict[str, float] = {}
    for e in items:
        day = e.ts.date().isoformat()
        min_share[day] = min(min_share.get(day, 1.0), e.cap_share)
        if e.kind in DAILY_CAP_FOR_KIND:
            counts[(day, e.kind)] += 1
        if e.cash_usd != 0:
            report.violations.append(f"{e.ts.isoformat()}: cash_usd {e.cash_usd} recorded")
        if e.kind == "claude_agent_run":
            cap = effective_cap(budget, "claude_agent_max_turns", e.cap_share)
            if e.turns is None:
                report.violations.append(f"{e.ts.isoformat()}: agent run without turn count")
            elif e.turns > cap:
                report.violations.append(f"{e.ts.isoformat()}: {e.turns} turns > cap {cap:g}")
        if e.kind == "local_call" and e.params_b is not None:
            cap = budget.caps["local_model_max_params_b"]
            if e.params_b > cap:
                report.violations.append(
                    f"{e.ts.isoformat()}: local model {e.params_b:g}B > cap {cap:g}B"
                )
    by_key = {(w.day, w.kind): w for w in waivers}
    used: set[tuple[str, str]] = set()
    for (day, kind), n in sorted(counts.items()):
        cap = effective_cap(budget, DAILY_CAP_FOR_KIND[kind], min_share[day])
        if n <= cap:
            continue
        overrun = f"{day}: {n} {kind} entries > cap {cap:g}"
        w = by_key.get((day, kind))
        if w is None:
            report.violations.append(overrun)
        elif n > w.max_entries:
            used.add((day, kind))
            report.violations.append(
                f"{overrun}, beyond waiver {w.decision}, which excuses at most {w.max_entries}"
            )
        else:
            used.add((day, kind))
            approval = f"approved by {w.approved_by} on {w.approved_on}"
            report.waived.append(f"{overrun}, waived by {w.decision} ({approval})")
            report.waived_by.append(w.decision)
    for key, w in sorted(by_key.items()):
        if key not in used:
            report.violations.append(f"waiver {w.decision} excuses no overrun ({key[0]} {key[1]})")
    report.daily_counts = dict(counts)
    return report


def calls_remaining(
    budget: Budget,
    entries: Iterable[LedgerEntry],
    kind: str,
    day: str,
    cap_share: float = 1.0,
) -> int:
    """Return how many more calls of ``kind`` the cap allows on ``day``.

    Args:
        budget: The validated budget.
        entries: Ledger entries so far.
        kind: A kind listed in :data:`DAILY_CAP_FOR_KIND`.
        day: ISO date (UTC) to count.
        cap_share: The ``CAP_SHARE`` value in effect now.

    Returns:
        Remaining calls, never negative.

    Raises:
        BudgetError: If ``kind`` has no per-day cap.
    """
    if kind not in DAILY_CAP_FOR_KIND:
        raise BudgetError(f"kind {kind!r} has no per-day cap")
    used = sum(1 for e in entries if e.kind == kind and e.ts.date().isoformat() == day)
    cap = effective_cap(budget, DAILY_CAP_FOR_KIND[kind], cap_share)
    return max(int(cap) - used, 0)


def format_report(budget: Budget, report: BudgetReport) -> list[str]:
    """Render the report printed by ``make budget``.

    Args:
        budget: The validated budget.
        report: The ledger check result.

    Returns:
        Output lines. The first is ``cash_usd = 0`` when no cash was spent.
    """
    cash = "0" if report.cash_usd == 0 else f"{report.cash_usd:g}"
    lines = [
        f"cash_usd = {cash}",
        f"plan_equivalent_usd = {report.plan_equivalent_usd:.4f} (API-price estimate, not cash)",
        f"ledger_entries = {report.entries}",
    ]
    lines.extend(f"cap {key} = {value:g}" for key, value in budget.caps.items())
    lines.extend(
        f"used {day} {kind} = {n}" for (day, kind), n in sorted(report.daily_counts.items())
    )
    lines.extend(f"WAIVED {w}" for w in report.waived)
    if not report.ok:
        lines.extend(f"VIOLATION {v}" for v in report.violations)
        lines.append("caps: VIOLATED")
    elif report.waived:
        lines.append(f"caps: respected except waived overruns ({', '.join(report.waived_by)})")
    else:
        lines.append("caps: all respected")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Print the budget report; exit non-zero on any violation."""
    parser = argparse.ArgumentParser(prog="baserate budget", description=__doc__)
    parser.add_argument("--budget", type=Path, default=Path("budget.toml"))
    parser.add_argument("--ledger", type=Path, default=Path("artifacts/usage/ledger.jsonl"))
    parser.add_argument(
        "--waivers",
        type=Path,
        default=None,
        help="accepted past overruns (default: budget_waivers.toml beside --budget)",
    )
    args = parser.parse_args(argv)
    waivers_path = args.waivers or args.budget.parent / "budget_waivers.toml"
    try:
        budget = load_budget(args.budget)
        report = check_ledger(budget, read_ledger(args.ledger), load_waivers(waivers_path))
    except BudgetError as exc:
        print(f"budget error: {exc}")
        print("caps: VIOLATED")
        return 1
    for line in format_report(budget, report):
        print(line)
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
