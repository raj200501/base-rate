goal_turn: 5
STATE: ACTIVE

# Status

Updated 2026-11-11 (resumed after Raj's pause of 2026-10-23). History: docs/LOG.md.

## Gates

Final sequence outcome (brief 10.6, recorded at the start of this iteration): at commit
19dbd5a the full `make ship-check` printed SHIP-CHECK: PASS 14/14, the latest `make review`
ended REVIEW: SHIP, and the local tag v1.0 points there (not pushed; tags are never pushed).
That pass is qualified: G14 passed on a ledger that omitted the day's two review runs, which
exceed the review-run cap (D25); Raj has since accepted that overrun (D26). The release
sprint then revised README.md, so G12 needs a fresh review of the new commit.

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
| G12 Independent review | SHIP for c433208 | fresh make review of the release commit (next day's allowance) |
| G13 Documentation | passes | |
| G14 Hygiene | passes; the 2026-11-11 review-run overrun (D25) is waived by Raj (D26) | |

## Now

Raj merged the release commit into main and made the repository public; this branch
fast-forwarded to that merge. G14 passes: Raj accepted the 2026-11-11 review-run overrun
(D25) with a dated waiver (D26) that make budget and G14 honor only through an exact
approval record in DECISIONS.md; every other overrun fails. A development QA pass over
every change since the last SHIP review found README claims stated too broadly and gaps in
the waiver and cap checks; all were fixed with tests (CORRECTIONS.md, D28, D29): the cap
share is now recorded in budget.toml, Claude ledger entries must record their share, and
`make review-start` enforces and logs the daily review cap. G12 waits for the 2026-11-12
UTC review allowance; the review is scheduled for 00:05 UTC, and a heartbeat runs every
two hours.

## Next three actions

1. When the next day's review allowance opens (2026-11-12 UTC): `make review-start`, commit
   the ledger entry, then the reviewer subagent; fix any blocking findings (no second review
   that day), then the full make ship-check on a fresh clone; on PASS 14/14 move the local
   tag v1.0 to that commit.
2. Keep logging every Claude subagent run that counts against a cap when it happens (D9,
   D25).
3. Work the open non-blocking review issues (docs/REVIEW_LOG.md), starting with N1 (the
   false-positive-rate interval printed tighter than the benign runs can certify), then the
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
single calls and the two `make review` runs of 2026-11-11, all logged after the fact; the
second review run exceeded the day's review cap and is waived by Raj (D25, D26). Claude has
run only as subagents of this session (no headless children); D28 lists the development
workflows that are not logged per call. `make review-start` checks the day's review cap at
the share in force (budget.toml records 0.2, D29) and logs the run before the reviewer
subagent starts.

## Needs Raj

None of these blocks the Definition of Done. (Raj accepted the 2026-11-11 review-run
overrun on 2026-11-11: DECISIONS.md D26.)

1. **Check one reference by hand.** The OpenAI post "How we monitor internal coding
   agents for misalignment"
   (https://openai.com/index/how-we-monitor-internal-coding-agents-misalignment) served
   a bot challenge to scripted access twice. Open it in a browser and confirm the
   title, date, and the brief's description; until then it is not cited.
2. **Claude arms (optional).** Claude subject runs and Claude-judge monitors need a
   machine where `claude` is logged in to the Max plan with none of
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, or
   `CLAUDE_CODE_USE_*` set. The study fields four families without them.
3. **Sandbox (optional).** A running Docker daemon or bubblewrap would allow
   third-party code; until then only this project's own tasks with in-process mock
   tools run.

## Blocked

Nothing.

## Backlog (suggestions for Raj, not ladder rungs)

- A machine-readable monitor report card (`baserate card --json`) that states the
  certification gap in words a safety case can quote (being built).
- A recorded-fixture adapter for real agent transcripts (Claude Code stream-json,
  Inspect logs), so a lab can score its own traffic with the same estimators.
