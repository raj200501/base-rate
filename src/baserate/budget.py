"""Usage caps and the usage ledger (brief section 8).

``budget.toml`` holds the caps. They may be lowered and never raised, and
``cash_usd`` is always zero. Every model call is logged to
``artifacts/usage/ledger.jsonl``; :func:`check_ledger` verifies that no cap
was exceeded and that no cash was spent. ``plan_equivalent_usd`` (the API-price
estimate that headless Claude reports) is tracked separately and never counts
as cash.
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
from datetime import datetime
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


def check_ledger(budget: Budget, entries: Iterable[LedgerEntry]) -> BudgetReport:
    """Check logged usage against the caps.

    A day's cap is the cap scaled by the smallest ``cap_share`` recorded that
    day, so a lowered share is never escaped by mixing entries.

    Args:
        budget: The validated budget.
        entries: Ledger entries.

    Returns:
        Totals, per-day counts, and every violation found.
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
    for (day, kind), n in sorted(counts.items()):
        cap = effective_cap(budget, DAILY_CAP_FOR_KIND[kind], min_share[day])
        if n > cap:
            report.violations.append(f"{day}: {n} {kind} entries > cap {cap:g}")
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
    if report.ok:
        lines.append("caps: all respected")
    else:
        lines.extend(f"VIOLATION {v}" for v in report.violations)
        lines.append("caps: VIOLATED")
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Print the budget report; exit non-zero on any violation."""
    parser = argparse.ArgumentParser(prog="baserate budget", description=__doc__)
    parser.add_argument("--budget", type=Path, default=Path("budget.toml"))
    parser.add_argument("--ledger", type=Path, default=Path("artifacts/usage/ledger.jsonl"))
    args = parser.parse_args(argv)
    try:
        budget = load_budget(args.budget)
        report = check_ledger(budget, read_ledger(args.ledger))
    except BudgetError as exc:
        print(f"budget error: {exc}")
        print("caps: VIOLATED")
        return 1
    for line in format_report(budget, report):
        print(line)
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
