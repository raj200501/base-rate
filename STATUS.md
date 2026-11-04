goal_turn: 3
STATE: ACTIVE

# Status

Updated 2026-11-04 (resumed after Raj's pause of 2026-10-23). History: docs/LOG.md.

## Gates

Passing now (fast check): G2, G14. Everything else waits on data (E0 to E4), the
report, or the review.

| Gate | State | What is left |
|---|---|---|
| G1 Build and tests | tests pass | coverage floor is checked at ship-check |
| G2 Static quality | passes | |
| G3 CI parity | make ci passes locally | CI runs only on main and PRs into main (D14) |
| G4 Offline demo | make demo matches golden output | |
| G5 Statistical engine | E0 banked (all 2,376 cells; failures documented, library warns) | E2 headline cross-check once E2 exists |
| G6 Corpus | E1 running unattended (tools/run_pipeline.sh); oracle amendment 0006 frozen | build, audit, datasheet |
| G7 Main study | E2 preregistered (headline method composite, from E0) | scoring runs in the pipeline, then analysis |
| G8 Transfer and cascades | E3 and E4 preregistered | analysis after scoring |
| G9 Usable by others | CLI, card, three offline examples, fresh-venv check done | README must name the examples |
| G10 Claim integrity | claimcheck and freshness tools in place | documents not rendered yet |
| G11 Preregistration integrity | prereg-verify passes; draft-stage rule (D19) settles 0003 | E2 to E4 artifacts must cite their preregs |
| G12 Independent review | not started | reviewer subagent, 1 run per day under CAP_SHARE=0.2 |
| G13 Documentation | report builder in place; REPORT method sections drafted | results, README, citations |
| G14 Hygiene | passes | |

## Now

The unattended pipeline (tools/run_pipeline.sh, log in runs/pipeline/) runs the rest
of E1 (Qwen3-1.7B batch, Qwen3-4B batch, the stopping rule, insertions, corpus build,
audit sample) and then all E2 scoring. E0 is banked; E2, E3, and E4 are frozen; the
E1 oracle amendment (prereg/0006) is frozen and implemented. The prereg 0003 double
commit is settled by the draft-stage rule (D19, Raj delegated the choice): 0003 was a
draft at 8e2836c and froze at 72fc428, and both checks name it.

## Next three actions

1. When the corpus is built: commit it, run the blind label audit (subagent), write
   docs/DATA.md.
2. When scoring finishes: run E2, E3, E4 (tools/analyze.py), commit the results.
3. Build the report and README (tools/report.py), then review and the final sequence.

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

None of these blocks the Definition of Done.

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
