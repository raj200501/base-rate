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
