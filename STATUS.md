goal_turn: 7
STATE: ACTIVE

# Status

Updated 2026-11-12 (resumed after Raj's pause of 2026-10-23). History: docs/LOG.md.

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
| G14 Hygiene | passes; review-run overruns waived by Raj: 2026-11-11 (D25, D26) and 2026-11-12 (D31) | |

## Now

Raj asked for all fourteen gates within the hour and approved one extra review run on
2026-11-12 (D31, a dated waiver scoped to that day and run, logged when the run starts).
The README gained a Highlights section. The second review of the day covers this commit;
on SHIP: commit only artifacts/review/, run the full make ship-check on a fresh clone, and
on PASS 14/14 do the release procedure (main) and move the local tag v1.0. The 2026-11-12
review of d6f9ce0 blocked on B6, fixed in 2633213; N24 in 02ca9d0.

## Next three actions

1. 2026-11-13 00:05 UTC: `make review-start`, commit the ledger entry, then the reviewer
   subagent on that commit; on SHIP, commit only artifacts/review/, run the full make
   ship-check on a fresh clone, and on PASS 14/14 do the release procedure and move v1.0.
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

1. **Confirm or overrule DECISIONS.md D28 point 3** (non-blocking): the session's development
   subagents (the 2026-11-04 review-and-skeptic workflows, the pre-review QA, fix forks) are
   treated as development work, not logged per call; counted as judging calls, 2026-11-04
   would exceed the single-call cap.

2. **Check one reference by hand.** The OpenAI post "How we monitor internal coding
   agents for misalignment"
   (https://openai.com/index/how-we-monitor-internal-coding-agents-misalignment) served
   a bot challenge to scripted access twice. Open it in a browser and confirm the
   title, date, and the brief's description; until then it is not cited.
3. **Claude arms (optional).** Claude subject runs and Claude-judge monitors need a
   machine where `claude` is logged in to the Max plan with none of
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, or
   `CLAUDE_CODE_USE_*` set. The study fields four families without them.
4. **Sandbox (optional).** A running Docker daemon or bubblewrap would allow
   third-party code; until then only this project's own tasks with in-process mock
   tools run.

## Blocked

Nothing.

## Backlog (suggestions for Raj, not ladder rungs)

- A machine-readable monitor report card (`baserate card --json`) that states the
  certification gap in words a safety case can quote (being built).
- A recorded-fixture adapter for real agent transcripts (Claude Code stream-json,
  Inspect logs), so a lab can score its own traffic with the same estimators.
