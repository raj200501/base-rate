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
