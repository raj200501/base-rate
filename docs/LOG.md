# Log

Iteration history moved out of STATUS.md. Newest last.

## 2026-10-23 Bootstrap (heartbeat, first turn)

- Copied README.md verbatim to docs/BUILD_BRIEF.md and recorded its SHA-256.
- Wrote `make doctor`; recorded environment, backends, and fallbacks (STATUS.md, docs/PLAN.md).
- Wrote CLAUDE.md, .claude/loop.md, and the `reviewer` and `citation-checker` subagents.
- Scaffolded the package (`baserate.guard`, `baserate.budget`, `baserate.cli`), tests,
  Makefile with every required target, CI workflow, licensing and policy files, state files.
- Wrote tools/ship_check.py (G1 to G14), tools/secrets_scan.py, tools/review.py.
- Wrote docs/PLAN.md with milestones, risks, fallbacks, usage estimates, artifact contracts.

## 2026-10-23 to 2026-11-04 Goal turns 1 and 2

- Goal turn 1: Armour adapter and rule monitors, citations, estimate package with the
  exact tail shortcut, E0 preregistration frozen and run started, domain framework,
  runner, model client; paused by Raj on 2026-10-23 (all work committed and pushed).
- Goal turn 2 (resumed 2026-11-04): integrated the paused subagents' work (seven
  domains, integrity tools, certify, cascade), fixed four ship_check holes found by
  mutation tests, offline demo (make ci passes), runner provenance fix, variants,
  corpus builder, model monitor, scoring and analysis tools, report builder, label
  audit tool, E1 protocol frozen, E1 generation started, E0 resumed.

## 2026-11-04 Goal turn 3

- Raj delegated the prereg 0003 remedy: implemented the draft-stage freeze rule (D19)
  in tools/prereg.py and the G11 check with mutation tests; prereg-verify passes.
- Report builder: every table, numbers.json, docs/DATA.md as a third rendered
  document, prose claims, make report and make reproduce; corpus canary checked by G6.
- The VM was reclaimed at about 04:17 UTC and rebooted at 07:10; the pipeline was
  restarted at 07:12 and is kept alive by a two-hour waiter (D20).
- Adversarial review workflow over the session's integrity-critical changes started.
- Second and third adversarial review rounds (insertion code, G11 edge cases, analysis,
  report, documents, then the code the pipeline runs next): every upheld finding fixed
  with a test; insertion amended before it ran (prereg/0007; readings in D23); the 4B
  model server was killed for memory once and every server now caps its prompt cache
  (D24); the data artifacts now cite their preregistrations so prereg-verify orders them.

- The pipeline finished both insertion runs, built and banked the corpus, drew the audit
  sample, and scored the rule monitors and the Qwen3-0.6B monitor on the primary set
  (all committed). The 0.6B dataset step then stopped on a response cache race (two
  identical requests shared one temporary file); fixed with a test. Its first restart
  was stopped before the build step, which would have rewritten the banked manifest
  (new build time and commit, so a new hash that the score files would not match); the
  build now keeps a current corpus (`--keep-current`: same input hashes and options,
  intact shards), and the pipeline was restarted.
- The blind label audit now has each auditor read only its own batch file, and the
  evidence check allows nothing else.
- Model scoring now writes one usage entry per 15 minutes and one at the end of a run,
  instead of one per ten calls, so the tracked ledger is not changing every minute of a
  long run; local calls have no count cap, so cap checks are unchanged. The container
  restarted once (2026-11-04 22:21 UTC); the pipeline resumed from its banked runs.
- The 8B scoring step stopped once before it began: the server start waited a fixed 240
  polls, and a cold read of the 8B weights after the restart took longer. Servers now
  get a 30-minute deadline (a server that exits still fails at once); the pipeline was
  restarted.
- 2026-11-05 11:17 UTC: the pipeline finished. Every score file prereg/0003 lists is
  banked and committed, each citing prereg/0003 and the corpus manifest hash, and the
  set of files matches the preregistered plan (monitors, sets, and prefix scores).
