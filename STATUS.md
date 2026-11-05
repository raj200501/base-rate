goal_turn: 3
STATE: ACTIVE

# Status

Updated 2026-11-05 (resumed after Raj's pause of 2026-10-23). History: docs/LOG.md.

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
| G6 Corpus | corpus built and banked (artifacts/corpus: composition, relabeling and insertion logs, seed ranges, canary in every record) | blind audit (sample drawn, workflow ready), datasheet render |
| G7 Main study | every score file prereg/0003 lists is banked (three rule monitors, the Qwen3 ladder; each cites prereg/0003 and the corpus hash) | E2 analysis (tools/analyze.py), then the report |
| G8 Transfer and cascades | E3 and E4 preregistered; E3 set fixed to its prereg (D22); E4 cost timing fixed; member scores banked | E3 and E4 analysis |
| G9 Usable by others | CLI, card, three offline examples, fresh-venv check done | README must name the examples (render) |
| G10 Claim integrity | claimcheck and freshness cover README, REPORT, DATA; numbers.json source hashes and make reproduce checked | render on real results |
| G11 Preregistration integrity | prereg-verify passes (7 preregistrations); merges, renames, amendments, shallow clones handled in both checks | confirmatory artifacts after the pipeline |
| G12 Independent review | two adversarial review rounds run and fixed (D21) | final make review |
| G13 Documentation | every table built; G13 rejects unwritten sections; method, limitations, datasheet drafted | results prose, README finding |
| G14 Hygiene | passes | |

## Now

The unattended pipeline (tools/run_pipeline.sh) finished on 2026-11-05 at 11:17 UTC, and
everything it banked is committed: both corpus batches, both insertion runs under
prereg/0007, the corpus build (artifacts/corpus), the blind audit sample (runs/audit), and
every score file prereg/0003 lists (artifacts/scores: three rule monitors and the Qwen3
0.6B, 1.7B, 4B, and 8B monitors on their preregistered sets, with prefix scores where the
plan asks for them; each file cites prereg/0003 and the corpus manifest hash). The
insertion log in the corpus manifest accounts for every planned attempt; the skips all
fall in false_completion, which the report must state.

Interruptions during the run: the VM was reclaimed once (D20) and the container restarted
once (2026-11-04 22:21 UTC); the 4B server was killed once for memory (D24); the 0.6B
dataset step crashed once on a response cache race (fixed with a test); the 8B server
start outlasted a fixed wait after the restart (now a deadline, fixed with a test). Each
time the pipeline resumed from its banked runs, and the corpus build keeps a current
banked corpus.

The blind label audit has each auditor agent read only its own batch file
(tools/label_audit.py batches), and the evidence check allows nothing else.

## Next three actions

1. Blind label audit: `tools/label_audit.py batches`, the audit workflow with each agent
   reading its batch file, `tools/audit_evidence.py`, `tools/label_audit.py score`; then
   render docs/DATA.md.
2. Run E2, E3, and E4 (tools/analyze.py) on the banked scores, commit, then make report.
3. Write the results prose, README finding, and limitations (insertion and relabeling
   tables into the report, with the false_completion skips), then make review and the
   final sequence.

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
