"""Independent review (``make review``).

When headless Claude is usable (claude CLI present, billing guard clean, daily
review cap not used up), runs the ``reviewer`` agent through
``claude -p --model opus`` in a fresh clone, with the per-run turn cap, and
writes ``artifacts/review/latest.json``. Otherwise prints why and points to the
subagent path, which starts with ``make review-start`` (``--start-subagent``):
that checks the current UTC day's review cap at ``CAP_SHARE`` and, only if a run
remains, logs the ``claude_review_run`` entry before the ``reviewer`` subagent
runs, so the entry lands in the commit the review covers (DECISIONS.md D9, D27).
A day whose cap is used up gets no review. The subagent writes
``artifacts/review/latest.json`` itself; the main session never edits it.

Either way it then validates ``artifacts/review/latest.json`` and prints its
verdict. The last line is ``REVIEW: SHIP``, ``REVIEW: BLOCK``, ``REVIEW: STALE``
(the reviewed commit differs from HEAD outside the allowed paths), or
``REVIEW: MISSING``/``REVIEW: INVALID``. Exit 0 only for a current SHIP.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from baserate import budget as budget_mod  # noqa: E402
from baserate.guard import current_problems  # noqa: E402

LATEST = Path("artifacts/review/latest.json")
HISTORY = Path("artifacts/review/history")
LEDGER = Path("artifacts/usage/ledger.jsonl")
SCHEMA = "baserate-review/1"
# The reviewed commit may differ from HEAD only in these paths (brief 10.1 step 8).
ALLOWED_DRIFT_FILES = ("STATUS.md", "docs/LOG.md")
ALLOWED_DRIFT_DIRS = ("artifacts/review/",)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REVIEWER_TOOLS = "Read,Grep,Glob"


def git(root: Path, *args: str) -> str:
    """Run git in ``root`` and return stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def validate(review: Any) -> list[str]:
    """Return schema problems in a parsed review (empty means valid)."""
    if not isinstance(review, dict):
        return ["review is not a JSON object"]
    problems = []
    if review.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    if not SHA_RE.match(str(review.get("reviewed_commit", ""))):
        problems.append("reviewed_commit must be a full 40-character SHA")
    if review.get("reviewer") not in {"headless-opus", "subagent"}:
        problems.append("reviewer must be headless-opus or subagent")
    verdict = review.get("verdict")
    if verdict not in {"SHIP", "BLOCK"}:
        problems.append("verdict must be SHIP or BLOCK")
    blocking = review.get("blocking")
    non_blocking = review.get("non_blocking")
    if not isinstance(blocking, list) or not isinstance(non_blocking, list):
        problems.append("blocking and non_blocking must be lists")
        return problems
    for item in blocking:
        if not isinstance(item, dict) or not all(
            str(item.get(k, "")).strip() for k in ("id", "title", "location", "evidence")
        ):
            problems.append("each blocking issue needs id, title, location, and evidence")
            break
    if any(not isinstance(i, dict) or not str(i.get("id", "")).strip() for i in non_blocking):
        problems.append("each non-blocking issue needs an id")
    if verdict == "SHIP" and blocking:
        problems.append("verdict SHIP with open blocking issues")
    if verdict == "BLOCK" and not blocking:
        problems.append("verdict BLOCK without a blocking issue")
    if review.get("final_line") != f"REVIEW: {verdict}":
        problems.append("final_line must equal 'REVIEW: <verdict>'")
    return problems


def drift_outside_allowed(root: Path, reviewed: str) -> list[str] | None:
    """Paths changed between ``reviewed`` and HEAD outside the allowed set.

    Returns None when ``reviewed`` is not an ancestor of HEAD.
    """
    try:
        git(root, "merge-base", "--is-ancestor", reviewed, "HEAD")
    except subprocess.CalledProcessError:
        return None
    changed = git(root, "diff", "--name-only", reviewed, "HEAD").splitlines()
    return [
        p
        for p in changed
        if p not in ALLOWED_DRIFT_FILES and not any(p.startswith(d) for d in ALLOWED_DRIFT_DIRS)
    ]


def check_latest(root: Path) -> tuple[str, list[str]]:
    """Validate the latest review against HEAD.

    Returns:
        (final line, detail lines).
    """
    path = root / LATEST
    if not path.exists():
        return "REVIEW: MISSING", [f"{LATEST} does not exist"]
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return "REVIEW: INVALID", [f"{LATEST} is not valid JSON"]
    problems = validate(review)
    if problems:
        return "REVIEW: INVALID", problems
    reviewed = review["reviewed_commit"]
    details = [
        f"reviewed_commit {reviewed} by {review['reviewer']} at {review.get('reviewed_at', '?')}",
        f"blocking {len(review['blocking'])}, non_blocking {len(review['non_blocking'])}",
    ]
    details.extend(
        f"BLOCKING {b['id']}: {b['title']} ({b['location']})" for b in review["blocking"]
    )
    drift = drift_outside_allowed(root, reviewed)
    if drift is None:
        return "REVIEW: STALE", [*details, "reviewed commit is not an ancestor of HEAD"]
    if drift:
        return "REVIEW: STALE", [*details, "changed since review: " + ", ".join(drift[:20])]
    log = root / "docs" / "REVIEW_LOG.md"
    log_text = log.read_text(encoding="utf-8") if log.exists() else ""
    missing = [
        i["id"]
        for i in review["non_blocking"]
        if re.search(rf"(?<![\w-]){re.escape(str(i['id']))}(?![\w-])", log_text) is None
    ]
    if missing:
        # The main session logs these (gate G12 checks the log separately).
        details.append("log these non-blocking issues in docs/REVIEW_LOG.md: " + ", ".join(missing))
    return f"REVIEW: {review['verdict']}", details


def headless_blockers(root: Path) -> list[str]:
    """Reasons headless review cannot run now (empty means it can)."""
    reasons = [f"billing guard: {p}" for p in current_problems(root)]
    if shutil.which("claude") is None:
        reasons.append("claude CLI not found")
    try:
        caps = budget_mod.load_budget(root / "budget.toml")
        entries = budget_mod.read_ledger(root / LEDGER)
    except budget_mod.BudgetError as exc:
        return [*reasons, f"budget: {exc}"]
    try:
        share = cap_share(root)
    except ValueError as exc:
        return [*reasons, str(exc)]
    today = datetime.now(UTC).date().isoformat()
    if budget_mod.calls_remaining(caps, entries, "claude_review_run", today, share) <= 0:
        reasons.append("daily review cap used up")
    return reasons


def cap_share(root: Path) -> float:
    """The cap share in force: budget.toml's ``cap_share``, lowered by ``CAP_SHARE`` if set.

    Raises:
        ValueError: If either is not a finite number in (0, 1] (BudgetError is a ValueError).
    """
    raw = os.environ.get("CAP_SHARE")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if not (math.isfinite(value) and 0 < value <= 1):
            raise ValueError(f"CAP_SHARE must be a number in (0, 1], got {raw!r}")
    caps = budget_mod.load_budget(root / "budget.toml")
    return budget_mod.share_in_force(caps, raw)


def headless_turn_cap(root: Path) -> int:
    """The per-run turn cap (``claude_agent_max_turns``) at ``CAP_SHARE``.

    Brief section 8 sets it per headless run, which a headless review is.
    """
    caps = budget_mod.load_budget(root / "budget.toml")
    return int(budget_mod.effective_cap(caps, "claude_agent_max_turns", cap_share(root)))


def start_subagent(root: Path, now: datetime | None = None) -> int:
    """Check today's review cap and log this reviewer-subagent run (``make review-start``).

    The main session runs this before it launches the ``reviewer`` subagent. If
    the current UTC day's ``claude_review_run`` cap at ``CAP_SHARE`` is used up,
    or the budget files are invalid, or the ledger already breaks a cap, it logs
    nothing and returns non-zero: no review runs. Otherwise it appends one
    ``claude_review_run`` entry now, so the entry is part of the commit the
    review then covers, and returns 0.
    """
    now = (now or datetime.now(UTC)).astimezone(UTC)
    today = now.date().isoformat()
    try:
        cap_share(root)  # validates CAP_SHARE with its own message
        result = budget_mod.audit(root, os.environ.get("CAP_SHARE"), ledger=root / LEDGER)
    except (ValueError, budget_mod.BudgetError) as exc:
        print(f"cannot start a review: {exc}; no review runs")
        return 2
    caps, entries, share, report = result.budget, result.entries, result.share, result.report
    if not report.ok:
        print(f"make budget fails ({report.violations[0]}); no review runs")
        return 1
    cap = budget_mod.effective_cap(caps, "claude_review_runs_per_day", share)
    used = sum(
        1 for e in entries if e.kind == "claude_review_run" and budget_mod.utc_day(e.ts) == today
    )
    if budget_mod.calls_remaining(caps, entries, "claude_review_run", today, share) <= 0:
        print(
            f"daily review cap used up: {used} claude_review_run on {today} (UTC), cap {cap:g} "
            f"(claude_review_runs_per_day at CAP_SHARE={share:g}); no review runs today"
        )
        return 1
    entry = {
        "ts": now.isoformat(timespec="seconds"),
        "kind": "claude_review_run",
        "backend": "claude-subagent",
        "purpose": "make review",
        "cash_usd": 0,
        "plan_equivalent_usd": 0,
        "cap_share": share,
        "note": (
            "reviewer subagent run, logged when the run starts so the entry lands in "
            "the commit the review covers (DECISIONS.md D9, D27)"
        ),
    }
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    print(
        f"logged claude_review_run {used + 1} of {cap:g} for {today} (UTC) at "
        f"CAP_SHARE={share:g}; commit the ledger, then run the `reviewer` subagent now "
        f"(.claude/agents/reviewer.md); it writes {LATEST}"
    )
    return 0


def run_headless(root: Path) -> None:
    """Run the reviewer agent headlessly in a fresh clone and bank its review."""
    agent = (root / ".claude" / "agents" / "reviewer.md").read_text(encoding="utf-8")
    body = agent.split("---", 2)[-1].strip()
    head = git(root, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="baserate-review-") as tmp:
        clone = Path(tmp) / "repo"
        subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True)
        prompt = (
            f"Review commit {head} of this repository as specified in your instructions. "
            f"The previous review, if any, is {LATEST}. Reply with only the JSON object."
        )
        cmd = headless_command(body, prompt, max_turns=headless_turn_cap(root))
        env = {
            k: v for k, v in os.environ.items() if k not in {"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"}
        }
        proc = subprocess.run(cmd, cwd=clone, env=env, capture_output=True, text=True, check=False)
    result_text, cost, turns = "", 0.0, None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            result_text = str(event.get("result", ""))
            cost = float(event.get("total_cost_usd", 0.0) or 0.0)
            if isinstance(event.get("num_turns"), int):
                turns = int(event["num_turns"])
    _append_ledger(root, cost, turns=turns)
    match = re.search(r"\{.*\}", result_text, flags=re.S)
    if not match:
        raise SystemExit("headless reviewer returned no JSON; see its output above")
    review = json.loads(match.group(0))
    review.update({"schema": SCHEMA, "reviewed_commit": head, "reviewer": "headless-opus"})
    review.setdefault("reviewed_at", datetime.now(UTC).isoformat(timespec="seconds"))
    _bank(root, review)


def headless_command(body: str, prompt: str, max_turns: int) -> list[str]:
    """The headless reviewer command, limited to ``max_turns`` agentic turns.

    ``--max-turns`` is Claude Code's documented print-mode turn limit.
    """
    return [
        "claude",
        "-p",
        "--model",
        "opus",
        "--output-format",
        "stream-json",
        "--verbose",
        "--restricted",
        "--strict-mcp-config",
        "--max-turns",
        str(max_turns),
        "--tools",
        REVIEWER_TOOLS,
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        REVIEWER_TOOLS,
        "--system-prompt",
        body,
        prompt,
    ]


def _append_ledger(root: Path, plan_equivalent_usd: float, turns: int | None = None) -> None:
    entry: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": "claude_review_run",
        "backend": "claude-plan",
        "model": "opus",
        "cash_usd": 0,
        "plan_equivalent_usd": plan_equivalent_usd,
        "cap_share": cap_share(root),
    }
    if turns is not None:
        entry["turns"] = turns
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _bank(root: Path, review: dict[str, Any]) -> None:
    latest = root / LATEST
    if latest.exists():
        old = json.loads(latest.read_text(encoding="utf-8"))
        (root / HISTORY).mkdir(parents=True, exist_ok=True)
        stamp = str(old.get("reviewed_at", "unknown")).replace(":", "")
        name = f"{stamp}-{str(old.get('reviewed_commit', 'x'))[:12]}.json"
        shutil.copy2(latest, root / HISTORY / name)
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Run (or request) a review, then validate and print the verdict."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true", help="never launch a reviewer")
    mode.add_argument(
        "--start-subagent",
        action="store_true",
        help="check today's review cap and log the reviewer-subagent run (make review-start)",
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.start_subagent:
        return start_subagent(root)
    if not args.validate_only:
        blockers = headless_blockers(root)
        if blockers:
            print("headless review unavailable: " + "; ".join(blockers))
            print(
                "the subagent path starts with `make review-start`, which checks today's "
                "review cap and logs the run; then run the `reviewer` subagent "
                f"(.claude/agents/reviewer.md), which writes {LATEST}"
            )
        else:
            run_headless(root)
    final, details = check_latest(root)
    for line in details:
        print(line)
    print(final)
    return 0 if final == "REVIEW: SHIP" else 1


if __name__ == "__main__":
    sys.exit(main())
