goal_turn: 2
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
| G5 Statistical engine | E0 running (final cells) | finalize, bank, link to E2 headlines |
| G6 Corpus | E1 generation running (Qwen3-1.7B batch) | 4B batch, insertions, variants, audit, datasheet |
| G7 Main study | E2 preregistration drafted | freeze after E0, score, analyze |
| G8 Transfer and cascades | E3 and E4 preregistrations drafted | freeze, analyze |
| G9 Usable by others | CLI, card, and examples being built by a subagent | README must name the examples |
| G10 Claim integrity | claimcheck and freshness tools in place | documents not rendered yet |
| G11 Preregistration integrity | fails: 0003 committed twice (Needs Raj 1) | Raj's decision; confirmatory artifacts |
| G12 Independent review | not started | reviewer subagent, 1 run per day under CAP_SHARE=0.2 |
| G13 Documentation | report builder in place; REPORT method sections drafted | results, README, citations |
| G14 Hygiene | passes | |

## Now

E0 confirmatory run (pinned worktree at the prereg commit) is finishing; E1 real agent
runs started with Qwen3-1.7B (2,000 planned runs; restart at full width when E0 frees
the CPU). Subagents: CLI, report card, and examples; certify and cascade tests.

## Next three actions

1. Finalize E0, choose the E2 headline method from the cells E0 supports, freeze the
   E2, E3, and E4 preregistrations (docs/drafts/) before any monitor score exists.
2. Finish E1: Qwen3-4B batch, controlled insertions, corpus build, blind label audit,
   docs/DATA.md datasheet.
3. Score the monitors (rules, Qwen3 ladder), run E2 to E4, build the report.

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

Item 1 blocks gate G11 (and therefore the Definition of Done); the others do not.

1. **Decide how to resolve the double commit of prereg/0003-e2-main-study.md**
   (CORRECTIONS.md, DECISIONS.md D18). Its first commit (8e2836c) carried the
   unfinished draft with the placeholder `TO_BE_SET_FROM_E0`; the next commit
   (72fc428, the same minute, before any monitor score) set the method to
   `composite`. G11 fails because a preregistration was edited after it was
   committed, and I will not change the check myself. Options, pick one:
   (a) allow a one-time rewrite and force-push of the unmerged branch
   `claude/tender-ptolemy-ilsn9p` that removes 0003 from commit 8e2836c, keeping
   the CORRECTIONS.md entry; (b) rule that a preregistration whose config holds
   an explicit `TO_BE_SET` placeholder is an unfrozen draft, so 0003 was frozen
   at 72fc428, and I implement exactly that rule in tools/prereg.py and
   tools/ship_check.py with mutation tests; or (c) leave G11 failing and ship
   with the deviation disclosed.
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
