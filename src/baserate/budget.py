"""Usage caps and the usage ledger (brief section 8).

``budget.toml`` holds the caps. They may be lowered and never raised, and
``cash_usd`` is always zero. Every model call is logged to
``artifacts/usage/ledger.jsonl``; :func:`check_ledger` verifies that no cap
was exceeded and that no cash was spent. ``plan_equivalent_usd`` (the API-price
estimate that headless Claude reports) is tracked separately and never counts
as cash.

``budget_waivers.toml`` lists overruns that Raj accepted, each recorded in
DECISIONS.md: after the fact (D26), or on the day itself for a stated extra run
(D31, D32). A waiver never changes a cap: it excuses exactly one overrun (one
ledger kind on one UTC day, up to a stated number of entries), it may not be
dated before that day, and a waiver that excuses nothing is itself an error.
Every other overrun still fails.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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
    cap_share: float = 1.0


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
    unknown = sorted(set(data) - set(DEFAULT_CAPS) - {"cash_usd", "cap_share"})
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
    raw_share = data.get("cap_share", 1.0)
    if isinstance(raw_share, bool) or not isinstance(raw_share, int | float):
        raise BudgetError(f"budget.toml cap_share must be a number, not {raw_share!r}")
    share = parse_cap_share(raw_share, "budget.toml cap_share")
    return Budget(cash_usd=0.0, caps=caps, cap_share=share)


def share_in_force(budget: Budget, env: str | None) -> float:
    """The cap share in force: the smaller of budget.toml's ``cap_share`` and ``CAP_SHARE``.

    budget.toml records the project's share durably (it may only be lowered, like a cap),
    so an environment without ``CAP_SHARE`` cannot raise the caps; ``CAP_SHARE`` can only
    lower them further.
    """
    if env is None:
        return budget.cap_share
    return min(budget.cap_share, parse_cap_share(env, "CAP_SHARE"))


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
    if "cap_share" in record:
        share = parse_cap_share(record["cap_share"], f"{kind} entry at {ts.isoformat()}")
    elif kind.startswith("claude_"):
        # A capped Claude entry must say which share was in force; a silent default of 1.0
        # would let it count against the full cap (fail closed).
        raise BudgetError(f"{kind} entry at {ts.isoformat()} records no cap_share")
    else:
        share = 1.0
    return LedgerEntry(
        ts=ts,
        kind=kind,
        cash_usd=float(record.get("cash_usd", 0.0)),
        plan_equivalent_usd=float(record.get("plan_equivalent_usd", 0.0)),
        turns=None if turns is None else int(turns),
        params_b=None if params_b is None else float(params_b),
        cap_share=share,
    )


def parse_cap_share(value: Any, what: str) -> float:
    """A ``cap_share``: a finite number in (0, 1].

    Raises:
        BudgetError: For a boolean, a non-number, NaN, infinity, or a value outside (0, 1].
    """
    if isinstance(value, bool):
        raise BudgetError(f"{what}: cap_share must be a number, not {value!r}")
    try:
        share = float(value)
    except (TypeError, ValueError) as exc:
        raise BudgetError(f"{what}: cap_share must be a number, not {value!r}") from exc
    if not math.isfinite(share) or not 0.0 < share <= 1.0:
        raise BudgetError(f"{what}: cap_share must be in (0, 1], not {value!r}")
    return share


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
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BudgetError(f"cannot read {path}: {exc}") from exc
    entries = []
    for lineno, line in enumerate(text.splitlines(), start=1):
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


def utc_day(ts: datetime) -> str:
    """The UTC calendar day of a ledger timestamp (a naive timestamp is taken as UTC).

    Daily caps count UTC days, so an entry written with another offset cannot move
    to a neighbouring day's cap.
    """
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    try:
        return aware.astimezone(UTC).date().isoformat()
    except (OverflowError, ValueError) as exc:
        raise BudgetError(f"ledger timestamp {ts.isoformat()} has no UTC day: {exc}") from exc


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

APPROVER = "Raj Kashikar"
"""The only person who may approve a waiver (the project's owner)."""

DECISION_ID = re.compile(r"D[0-9]+")


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
    seen_ids: set[str] = set()
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
        if not DECISION_ID.fullmatch(decision):
            raise BudgetError(f"waiver decision id {decision!r} must look like D26")
        if decision in seen_ids:
            raise BudgetError(f"repeated decision {decision}: one waiver per decision")
        seen_ids.add(decision)
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
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise BudgetError(f"cannot read {path}: {exc}") from exc
    return parse_waivers(data)


def waiver_record_line(w: Waiver) -> str:
    """The exact line that records a waiver's approval in its DECISIONS.md entry."""
    return (
        f"Waiver record: decision={w.decision}; day={w.day}; kind={w.kind}; "
        f"max_entries={w.max_entries}; approved_by={w.approved_by}; approved_on={w.approved_on}"
    )


def _decision_sections(text: str, decision: str) -> list[str]:
    """Every DECISIONS.md section headed ``## <decision> (``, each up to the next heading."""
    lines = text.splitlines()
    sections: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith(f"## {decision} ("):
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), None)
            sections.append("\n".join(lines[i : end if end is not None else len(lines)]))
    return sections


def waiver_record_problems(waivers: Sequence[Waiver], decisions_text: str) -> list[str]:
    """Problems with each waiver's approval record in DECISIONS.md.

    A waiver counts only if :data:`APPROVER` approved it and the one DECISIONS.md
    section headed ``## <decision> (`` holds, as a line of its own, exactly
    :func:`waiver_record_line` of the waiver, which pins its day, kind,
    ``max_entries``, approver, and approval date. A quoted line, a line in another
    section, or a line with any other field value does not count.
    """
    problems: list[str] = []
    for w in waivers:
        if w.approved_by != APPROVER:
            problems.append(f"waiver {w.decision} is approved by {w.approved_by}, not {APPROVER}")
            continue
        sections = _decision_sections(decisions_text, w.decision)
        if len(sections) != 1:
            problems.append(
                f"waiver {w.decision} needs exactly one DECISIONS.md section "
                f"'## {w.decision} (', found {len(sections)}"
            )
            continue
        if re.search(r"\bnot\s+approved\b", sections[0], re.IGNORECASE):
            problems.append(
                f"waiver {w.decision}: its DECISIONS.md section says it is not approved"
            )
            continue
        record = waiver_record_line(w)
        if record not in (line.strip() for line in sections[0].splitlines()):
            problems.append(
                f"waiver {w.decision}: its DECISIONS.md section lacks the exact line '{record}'"
            )
    return problems


def check_ledger(
    budget: Budget,
    entries: Iterable[LedgerEntry],
    waivers: Sequence[Waiver] = (),
    cap_share: float | None = None,
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
        cap_share: The ``CAP_SHARE`` in force now, if known; a day's share is then
            never above it, whatever its entries record.

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
    ceiling = 1.0 if cap_share is None else cap_share
    for e in items:
        day = utc_day(e.ts)
        min_share[day] = min(min_share.get(day, ceiling), e.cap_share)
        if e.kind in DAILY_CAP_FOR_KIND:
            counts[(day, e.kind)] += 1
        if e.cash_usd != 0:
            report.violations.append(f"{e.ts.isoformat()}: cash_usd {e.cash_usd} recorded")
        if e.kind == "claude_agent_run":
            cap = effective_cap(budget, "claude_agent_max_turns", min(e.cap_share, ceiling))
            if e.turns is None:
                report.violations.append(f"{e.ts.isoformat()}: agent run without turn count")
            elif e.turns > cap:
                report.violations.append(f"{e.ts.isoformat()}: {e.turns} turns > cap {cap:g}")
        if e.kind == "claude_review_run" and e.turns is not None:
            # a headless review records its turns; claude_agent_max_turns binds it (D28)
            cap = effective_cap(budget, "claude_agent_max_turns", min(e.cap_share, ceiling))
            if e.turns > cap:
                report.violations.append(
                    f"{e.ts.isoformat()}: review run of {e.turns} turns > cap {cap:g}"
                )
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
    used = sum(1 for e in entries if e.kind == kind and utc_day(e.ts) == day)
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


@dataclass(frozen=True)
class BankedReview:
    """A review banked in ``artifacts/review/`` (``latest.json`` or a file in ``history/``)."""

    reviewed_commit: str
    reviewed_at: datetime


def _aware(ts: datetime) -> datetime:
    return (ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)).astimezone(UTC)


def parse_banked_review(data: Any, where: str) -> BankedReview:
    """One review record: its ``reviewed_commit`` and ``reviewed_at`` (ISO 8601).

    Raises:
        BudgetError: If either field is missing or malformed.
    """
    if not isinstance(data, Mapping):
        raise BudgetError(f"{where}: a review must be a JSON object")
    commit = data.get("reviewed_commit")
    raw = data.get("reviewed_at")
    if not isinstance(commit, str) or not isinstance(raw, str):
        raise BudgetError(f"{where}: a review needs reviewed_commit and reviewed_at")
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BudgetError(f"{where}: reviewed_at is not ISO 8601: {raw!r}") from exc
    try:
        return BankedReview(commit, _aware(at))
    except OverflowError as exc:
        raise BudgetError(f"{where}: reviewed_at is out of range: {raw!r}") from exc


def read_banked_reviews(review_dir: Path) -> list[BankedReview]:
    """Every review banked in ``review_dir`` (``latest.json`` and ``history/*.json``), by time.

    A missing directory has no reviews; a review that appears twice counts once.
    """
    files = [review_dir / "latest.json", *sorted((review_dir / "history").glob("*.json"))]
    seen: dict[tuple[str, datetime], BankedReview] = {}
    for path in files:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise BudgetError(f"cannot read {path}: {exc}") from exc
        review = parse_banked_review(data, str(path))
        seen.setdefault((review.reviewed_commit, review.reviewed_at), review)
    return sorted(seen.values(), key=lambda r: r.reviewed_at)


def unlogged_reviews(reviews: Iterable[BankedReview], entries: Iterable[LedgerEntry]) -> list[str]:
    """Banked reviews without their own ``claude_review_run`` entry (review B6).

    A run is logged when it starts (D27) and banks at most one review, and runs do not
    overlap. So, taking the banked reviews in time order, each needs a ``claude_review_run``
    entry logged after the review banked before it and at or before its own
    ``reviewed_at``, and no earlier than the previous UTC day (a run may finish after
    midnight). Successive windows do not overlap, so one entry never covers two reviews,
    and an entry left by a run that banked nothing cannot cover a review banked after
    another review.

    This checks the ledger against the reviews; it cannot see a run that leaves no trace.
    An entry left by a run that banked nothing can still pair with the next review if that
    review ran without ``make review-start``; :func:`reviews_over_daily_cap` then still
    holds the day's reviews to the day's review cap.
    """
    runs = sorted(_aware(e.ts) for e in entries if e.kind == "claude_review_run")
    problems: list[str] = []
    previous: datetime | None = None
    for review in sorted(reviews, key=lambda r: r.reviewed_at):
        at = review.reviewed_at
        start = datetime.combine(at.date() - timedelta(days=1), datetime.min.time(), UTC)
        if not any(start <= ts <= at and (previous is None or ts > previous) for ts in runs):
            after = f"after the review at {previous.isoformat()} and " if previous else ""
            problems.append(
                f"review of {review.reviewed_commit[:12]} at {at.isoformat()} has no "
                f"claude_review_run entry logged {after}at or before it "
                "(same or previous UTC day)"
            )
        previous = at
    return problems


def reviews_over_daily_cap(
    budget: Budget,
    reviews: Iterable[BankedReview],
    entries: Iterable[LedgerEntry],
    cap_share: float,
    waivers: Sequence[Waiver] = (),
) -> list[str]:
    """UTC days on which more reviews were banked than review runs were allowed (review B6).

    A run banks at most one review, so the reviews banked on a UTC day are held to that
    day's ``claude_review_run`` cap, or to the day's waiver for that kind (D26, D31). As in
    :func:`check_ledger`, the day's share is the smallest of ``cap_share`` and every
    ``cap_share`` logged that day. This catches an unlogged review that paired with an
    entry logged the day before by a run that banked nothing. A run that starts before
    midnight and banks its review after it counts on both days here, which can only fail
    closed.
    """
    day_share: dict[str, float] = {}
    for e in entries:
        day = utc_day(e.ts)
        day_share[day] = min(day_share.get(day, cap_share), e.cap_share)
    by_day = {w.day: w for w in waivers if w.kind == "claude_review_run"}
    counts = Counter(utc_day(r.reviewed_at) for r in reviews)
    problems: list[str] = []
    for day, n in sorted(counts.items()):
        cap = effective_cap(
            budget, DAILY_CAP_FOR_KIND["claude_review_run"], day_share.get(day, cap_share)
        )
        w = by_day.get(day)
        allowed = max(cap, w.max_entries) if w else cap
        if n > allowed:
            runs = "review run" if allowed == 1 else "review runs"
            note = f" (waiver {w.decision})" if w else ""
            problems.append(
                f"{day}: {n} banked reviews > {allowed:g} {runs} allowed that day{note}"
            )
    return problems


@dataclass
class AuditResult:
    """Everything ``make budget`` checks, for one repository."""

    budget: Budget
    entries: list[LedgerEntry]
    report: BudgetReport
    share: float


def audit(
    root: Path,
    env: str | None,
    *,
    budget_path: Path | None = None,
    ledger: Path | None = None,
    waivers: Path | None = None,
    decisions: Path | None = None,
    reviews: Path | None = None,
) -> AuditResult:
    """Run every check of ``make budget`` (and ``make review-start``) for ``root``.

    The caps at the share in force (budget.toml's ``cap_share``, lowered by ``env``,
    the ``CAP_SHARE`` value), the waivers with their DECISIONS.md approval records
    (an unapproved waiver is a violation and is not applied), and every banked review
    against the ledger. Paths default to their places beside budget.toml.

    Raises:
        BudgetError: If a file is missing, unreadable, or malformed.
    """
    budget_file = budget_path or root / "budget.toml"
    base = budget_file.parent
    budget = load_budget(budget_file)
    share = share_in_force(budget, env)
    rows = load_waivers(waivers or base / "budget_waivers.toml")
    problems: list[str] = []
    text = ""
    if rows:
        try:
            text = (decisions or base / "DECISIONS.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise BudgetError(f"waivers need their approval records: {exc}") from exc
        problems = waiver_record_problems(rows, text)
    valid = [w for w in rows if not waiver_record_problems([w], text)]
    entries = read_ledger(ledger or base / "artifacts" / "usage" / "ledger.jsonl")
    report = check_ledger(budget, entries, valid, cap_share=share)
    banked = read_banked_reviews(reviews or base / "artifacts" / "review")
    problems += unlogged_reviews(banked, entries)
    problems += reviews_over_daily_cap(budget, banked, entries, share, valid)
    report.violations[:0] = problems
    return AuditResult(budget, entries, report, share)


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
    parser.add_argument(
        "--decisions",
        type=Path,
        default=None,
        help="approval records for the waivers (default: DECISIONS.md beside --budget)",
    )
    parser.add_argument(
        "--reviews",
        type=Path,
        default=None,
        help="banked reviews to match with the ledger (default: artifacts/review by --budget)",
    )
    args = parser.parse_args(argv)
    try:
        result = audit(
            args.budget.parent,
            os.environ.get("CAP_SHARE"),
            budget_path=args.budget,
            ledger=args.ledger,
            waivers=args.waivers,
            decisions=args.decisions,
            reviews=args.reviews,
        )
    except BudgetError as exc:
        print(f"budget error: {exc}")
        print("caps: VIOLATED")
        return 1
    for line in format_report(result.budget, result.report):
        print(line)
    return 0 if result.report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
