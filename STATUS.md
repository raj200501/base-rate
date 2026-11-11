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

Raj merged the release commit into main (a merge commit, same SHAs) and made the
repository public; this branch fast-forwarded to that merge. G14: Raj accepted the
2026-11-11 review-run overrun (D25) with a dated waiver scoped to that day and kind
(DECISIONS.md D26, budget_waivers.toml); make budget and G14 honor exactly that waiver,
tests show every other overrun still fails, and caps and CAP_SHARE=0.2 are unchanged. G12
waits for the next day's review allowance (2026-11-12 UTC).

## Next three actions

1. When the next day's review allowance opens (2026-11-12 UTC), run make review on the
   release commit, fix any blocking findings, then the full make ship-check on a fresh
   clone; on PASS 14/14 move the local tag v1.0 to that commit.
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

cash_usd = 0 (`make budget`). Local model calls only, logged per batch in
artifacts/usage/ledger.jsonl. Claude: subagents of this session only (no headless
children); reviewer runs will be logged against the daily review cap.

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
