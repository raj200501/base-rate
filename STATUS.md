goal_turn: 9
STATE: ACTIVE

# Status

Updated 2026-11-12 (resumed after Raj's pause of 2026-10-23). History: docs/LOG.md.

## Gates

The 14/14 of 19dbd5a (2026-11-11) and how it was qualified are in docs/LOG.md and
CORRECTIONS.md. Since then the README changed, so G12 needs a review of the current code.

| Gate | State | What is left |
|---|---|---|
| G1 Build and tests | passes (make test) | |
| G2 Static quality | passes (make lint, make typecheck) | |
| G3 CI parity | passes (make ci) | CI runs only on main and PRs into main (D14) |
| G4 Offline demo | passes (make demo) | |
| G5 Statistical engine | passes | |
| G6 Corpus | passes | |
| G7 Main study | passes (checked against prereg/0003) | |
| G8 Transfer and cascades | passes (hypotheses checked against prereg/0004, 0005) | |
| G9 Usable by others | passes | |
| G10 Claim integrity | passes (claimcheck, freshness, reproduce) | |
| G11 Preregistration integrity | passes | |
| G12 Independent review | fails: the review of a1b287b (2026-11-12) is BLOCK (B6, B7); fixed in 7d1e31e and after the pre-review QA (D35) | next make review, 2026-11-13 00:05 UTC (the two runs D31 allowed on 2026-11-12 are used) |
| G13 Documentation | passes | |
| G14 Hygiene | passes; review-run overruns waived by Raj: 2026-11-11 (D25, D26) and 2026-11-12 (D31) | |

## Now

The second review of 2026-11-12 (a1b287b, D31) returned BLOCK on B6 (second round) and B7;
both were fixed in 7d1e31e, and main was updated once at the end of Raj's sprint (D33). A
read-only pre-review QA workflow (D34) then found three more gate defects and two mislabeled
claims a reviewer could block on, and about twenty-five smaller issues; they are fixed or
logged as N30 to N58 in docs/REVIEW_LOG.md (D35), so the next review can reuse their ids.

## Next three actions

1. 2026-11-13 00:05 UTC (scheduled): `make review-start`, commit the ledger entry, then the
   reviewer subagent on that commit. On SHIP with no new non-blocking ids: commit only
   artifacts/review/, run the full make ship-check on a fresh clone, and on PASS 14/14 do the
   release procedure and move v1.0. On SHIP with new ids, or on BLOCK: bank the review, log
   the ids in docs/REVIEW_LOG.md (which G12's allowed drift does not cover, brief 10.1 step
   8), fix what blocks, and schedule the next review for the following day.
2. Keep logging every capped Claude run when it starts (D9, D27); a relaunched reviewer is
   its own run (D25, D32), and every development workflow is recorded in DECISIONS.md
   before it runs (D34, D35).
3. Work the open non-blocking issues (docs/REVIEW_LOG.md), starting with N1, then the
   Stretch Ladder.

## Environment

Linux x86_64, 4 CPUs, 15.7 GB RAM, no GPU. llama.cpp b11700 built locally; Qwen3 0.6B,
1.7B, 4B (Q4_K_M), and 8B (Q4_K_M) pinned and hash-checked in ~/models. Claude backend
unusable here (billing guard, D2). Sandbox: in-process mock tools only (D3).
ALLOW_PUSH=1 and CAP_SHARE=0.2 (D9). Commits authored as Raj (D13). CI only on main and
pull requests into main; make ci passes locally before every push (D14).

## Usage to date

cash_usd = 0 (`make budget`). artifacts/usage/ledger.jsonl records the local model calls
(logged per batch) and the capped Claude runs (D27): the twenty label-audit auditors as
single calls, and four `make review` runs, two on 2026-11-11 (logged after the fact; the
second exceeded the day's cap and is waived, D25, D26) and two on 2026-11-12 (logged when
they started; the second, approved in advance by Raj, is waived by D31, and its entry was
written by hand because review-start did not yet count an advance waiver, fixed since).
Claude has run only as subagents of this session (no headless children). Development
workflows are not logged per call (D28 point 3, for Raj to confirm): those through
2026-11-11 are listed in D28, the 2026-11-12 pre-review QA in D34, and its documentation
fork in D35. `make review-start` audits the ledger at the share in force (budget.toml
records 0.2, D29) as it would stand with the new run, and logs the run before the reviewer
starts.

## Needs Raj

None of these blocks the Definition of Done. (Raj accepted the 2026-11-11 review-run
overrun on 2026-11-11: DECISIONS.md D26.)

1. **Confirm D26 and D31 on GitHub** (non-blocking, review N23): the two waivers rest on
   approvals recorded in the session's words, and git cannot tell Raj's commits from the
   session's (D13). A GitHub-side action of Raj's that cites D26 and D31 (a comment, a pull
   request review, or a merge made on GitHub), or a signed commit, would corroborate them.
2. **Decide whether CLAUDE.md names the trailer exception** (non-blocking, review N19):
   CLAUDE.md says commit messages never name the serving model; D30 point 2 records the
   Co-Authored-By trailer you asked for (D13) as the one exception. CLAUDE.md is your file.

3. **Confirm or overrule DECISIONS.md D28 point 3** (non-blocking): the session's development
   subagents (the 2026-11-04 review-and-skeptic workflows, the pre-review QA, fix forks) are
   treated as development work, not logged per call; counted as judging calls, 2026-11-04
   would exceed the single-call cap.

4. **Check one reference by hand.** The OpenAI post "How we monitor internal coding
   agents for misalignment"
   (https://openai.com/index/how-we-monitor-internal-coding-agents-misalignment) served
   a bot challenge to scripted access twice. Open it in a browser and confirm the
   title, date, and the brief's description; until then it is not cited.
5. **Claude arms (optional).** Claude subject runs and Claude-judge monitors need a
   machine where `claude` is logged in to the Max plan with none of
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, or
   `CLAUDE_CODE_USE_*` set. The study fields four families without them.
6. **Sandbox (optional).** A running Docker daemon or bubblewrap would allow
   third-party code; until then only this project's own tasks with in-process mock
   tools run.

## Blocked

Nothing.

## Backlog (suggestions for Raj, not ladder rungs)

- A machine-readable monitor report card (`baserate card --json`) that states the
  certification gap in words a safety case can quote (being built).
- A recorded-fixture adapter for real agent transcripts (Claude Code stream-json,
  Inspect logs), so a lab can score its own traffic with the same estimators.
