goal_turn: 3
STATE: ACTIVE

# Status

Updated 2026-11-05 (resumed after Raj's pause of 2026-10-23). History: docs/LOG.md.

## Gates

All results are banked and rendered; the final sequence (brief 10.6) is under way: the
fast check, then make review, then the full make ship-check and the local v1.0 tag.

| Gate | State | What is left |
|---|---|---|
| G1 Build and tests | tests pass, core coverage above the floor | |
| G2 Static quality | passes (every type: ignore has a code and a reason) | |
| G3 CI parity | make ci passes locally | CI runs only on main and PRs into main (D14) |
| G4 Offline demo | make demo matches golden output | |
| G5 Statistical engine | E0 banked; E2 headline estimates list the E0 cells they rely on | |
| G6 Corpus | corpus, blind label audit (artifacts/corpus/label_audit.json), datasheet docs/DATA.md | |
| G7 Main study | E2 confirmatory results banked (artifacts/e2/results.json) | |
| G8 Transfer and cascades | E3 and E4 banked and reported, null results included | |
| G9 Usable by others | README names the three offline examples | |
| G10 Claim integrity | claimcheck and freshness pass on README, REPORT, DATA | |
| G11 Preregistration integrity | prereg-verify passes; every artifact cites an earlier frozen prereg | |
| G12 Independent review | three adversarial rounds fixed (D21) | final make review |
| G13 Documentation | README per section 11, REPORT complete | |
| G14 Hygiene | passes | |

## Now

Every experiment is banked and committed. E2 to E4 ran from the banked scores
(tools/analyze.py); the blind label audit ran as a Claude Code workflow in which each of
twenty auditors read only its own batch file of monitor views, and tools/label_audit.py
accepted it on that evidence. make report rendered README.md, docs/REPORT.md, and
docs/DATA.md from the artifacts; claimcheck and freshness pass. The headline is a negative
result for single monitors at realistic budgets (README first screen).

Fixes made while finishing: E2's independent-run count per estimate counted a source run
behind both an inserted violation and its benign trace twice (now the union; estimates
unchanged); the E4 table header carried untagged digits; the real-prose test fixture now
mirrors the study's monitors and cascade pairs.

## Next three actions

1. Final sequence (brief 10.6): make review with the reviewer subagent; on SHIP commit only
   artifacts/review/; run the full make ship-check; tag v1.0 locally on PASS 14/14.
2. On a BLOCK, fix every blocking issue in a later turn and start the sequence again.
3. After v1.0: the Stretch Ladder (brief section 7), starting with adapters for real agent
   transcripts.

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
