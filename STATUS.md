goal_turn: 3
STATE: ACTIVE

# Status

Updated 2026-11-04 (resumed after Raj's pause of 2026-10-23). History: docs/LOG.md.

## Gates

Passing now (fast check): G1, G2, G3, G4, G11, G14. Everything else waits on data
(E1 to E4), the report, or the review.

| Gate | State | What is left |
|---|---|---|
| G1 Build and tests | tests pass | coverage floor is checked at ship-check |
| G2 Static quality | passes | |
| G3 CI parity | make ci passes locally | CI runs only on main and PRs into main (D14) |
| G4 Offline demo | make demo matches golden output | |
| G5 Statistical engine | E0 banked (all 2,376 cells; failures documented, library warns) | E2 headline cross-check once E2 exists |
| G6 Corpus | 1.7B batch done (2,000 runs); 4B batch running; insertion amended (prereg/0007); canary in every record | insertions, build, blind audit (workflow ready), datasheet render |
| G7 Main study | E2 preregistered; score metadata cites prereg/0003; time to detection honors the tie fraction | scoring in the pipeline, then analysis |
| G8 Transfer and cascades | E3 and E4 preregistered; E3 set fixed to its prereg (D22); E4 cost timing fixed | analysis after scoring |
| G9 Usable by others | CLI, card, three offline examples, fresh-venv check done | README must name the examples (render) |
| G10 Claim integrity | claimcheck and freshness cover README, REPORT, DATA; numbers.json source hashes and make reproduce checked | render on real results |
| G11 Preregistration integrity | prereg-verify passes (7 preregistrations); merges, renames, amendments, shallow clones handled in both checks | confirmatory artifacts after the pipeline |
| G12 Independent review | two adversarial review rounds run and fixed (D21) | final make review |
| G13 Documentation | every table built; G13 rejects unwritten sections; method, limitations, datasheet drafted | results prose, README finding |
| G14 Hygiene | passes | |

## Now

The unattended pipeline (tools/run_pipeline.sh) is in the Qwen3-4B corpus batch; then
insertions under prereg/0007, the corpus build, the audit sample, and all E2 scoring. The
VM was reclaimed once (D20) and the 4B server was killed once for memory (D24); each time
the pipeline resumed from its banked runs, and a tracked waiter keeps the session active.
Two adversarial review rounds over this session's changes found and fixed defects in the
insertion code (prereg/0007), the G11 checks (merges, renames, amendments), the analysis
(time-to-detection ties, the E3 set), the report and reproduce tooling, and the documents.

## Next three actions

1. After the corpus build: commit it, run the blind label audit workflow
   (scratchpad audit.mjs), bank evidence with tools/audit_evidence.py and
   tools/label_audit.py score, render docs/DATA.md.
2. After scoring: run E2, E3, E4 (tools/analyze.py), commit, then make report.
3. Write the results prose, README finding and limitations (insertion and relabeling
   tables into the report), then make review and the final sequence.

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
