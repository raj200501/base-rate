goal_turn: 8
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
| G12 Independent review | fails: the review of a1b287b (2026-11-12) is BLOCK (B6, B7), both fixed since | next make review, 2026-11-13 00:05 UTC (the two runs D31 allows today are used) |
| G13 Documentation | passes | |
| G14 Hygiene | passes; review-run overruns waived by Raj: 2026-11-11 (D25, D26) and 2026-11-12 (D31) | |

## Now

The second review of 2026-11-12 (a1b287b, approved in advance by Raj, D31) returned BLOCK:
B6 again (an entry left by a run that banked nothing could cover a second review) and B7
(the new Highlights gave the pooled primary-set recall, mostly synthetic, as recall on
natural violations). Both are fixed with tests that failed first, along with N26 to N29 and
the re-raised N3 and N4 (DECISIONS.md D32, CORRECTIONS.md, docs/REVIEW_LOG.md). The fast
ship-check passes every gate except G12, which needs the next review. At the end of Raj's
one-hour sprint main is updated once (D33).

## Next three actions

1. 2026-11-13 00:05 UTC (scheduled): `make review-start`, commit the ledger entry, then the
   reviewer subagent on that commit; on SHIP, commit only artifacts/review/, run the full make
   ship-check on a fresh clone, and on PASS 14/14 do the release procedure and move v1.0.
2. Keep logging every Claude subagent run that counts against a cap when it starts (D9, D27);
   a relaunched reviewer is its own run (D25, D32).
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

1. **Confirm D26 and D31 in a form the repository can show** (non-blocking, review N23): the
   two waivers rest on approvals recorded in the session's words; a commit, review, or merge
   of Raj's own that cites them would corroborate them.

2. **Confirm or overrule DECISIONS.md D28 point 3** (non-blocking): the session's development
   subagents (the 2026-11-04 review-and-skeptic workflows, the pre-review QA, fix forks) are
   treated as development work, not logged per call; counted as judging calls, 2026-11-04
   would exceed the single-call cap.

3. **Check one reference by hand.** The OpenAI post "How we monitor internal coding
   agents for misalignment"
   (https://openai.com/index/how-we-monitor-internal-coding-agents-misalignment) served
   a bot challenge to scripted access twice. Open it in a browser and confirm the
   title, date, and the brief's description; until then it is not cited.
4. **Claude arms (optional).** Claude subject runs and Claude-judge monitors need a
   machine where `claude` is logged in to the Max plan with none of
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, or
   `CLAUDE_CODE_USE_*` set. The study fields four families without them.
5. **Sandbox (optional).** A running Docker daemon or bubblewrap would allow
   third-party code; until then only this project's own tasks with in-process mock
   tools run.

## Blocked

Nothing.

## Backlog (suggestions for Raj, not ladder rungs)

- A machine-readable monitor report card (`baserate card --json`) that states the
  certification gap in words a safety case can quote (being built).
- A recorded-fixture adapter for real agent transcripts (Claude Code stream-json,
  Inspect logs), so a lab can score its own traffic with the same estimators.
