goal_turn: 2
STATE: ACTIVE

# Status

Updated 2026-10-23 by the heartbeat (bootstrap turn). History: docs/LOG.md.

## Gates

From `python tools/ship_check.py --fast` on the bootstrap commit (fast runs do not count).

| Gate | Fast result | Evidence |
|---|---|---|
| G1 Build and tests | FAIL | tests pass; core packages traces, monitors, estimate, certify, cascade not built |
| G2 Static quality | PASS | ruff check, ruff format --check, mypy --strict clean |
| G3 CI parity | FAIL | workflow runs `make ci` on 3.11 and 3.12; `make demo` not built |
| G4 Offline demo | FAIL | demo and golden output not built |
| G5 Statistical engine | FAIL | no E0 artifact |
| G6 Corpus | FAIL | no corpus manifest |
| G7 Main study | FAIL | no E2 artifact |
| G8 Transfer and cascades | FAIL | no docs/REPORT.md, no E3 or E4 artifacts |
| G9 Usable by others | FAIL | examples not written |
| G10 Claim integrity | FAIL | no docs/REPORT.md; claimcheck and freshness not built |
| G11 Preregistration integrity | FAIL | no preregistrations |
| G12 Independent review | FAIL | no review yet |
| G13 Documentation | FAIL | README.md is still the build brief |
| G14 Hygiene | PASS | secrets-scan clean; cash_usd = 0; caps respected; tree clean |

## Now

Bootstrap complete. Next iteration starts milestone M1 (statistical core), the
critical path for G1, G3, G4, and G9.

## Next three actions

1. M1: `baserate.estimate` test-first: prevalence reweighting, exact (Clopper-Pearson),
   Jeffreys and Bayesian beta intervals, cluster bootstrap by source run, recall and
   precision at a review budget, stratified and importance-sampled estimators.
2. M1: `baserate.certify` (closed forms where they exist, simulation otherwise) and
   `baserate.cascade` (threshold optimization under cost and review budgets, Pareto
   fronts); CLI `eval`, `certify`, `cascade`, `card`.
3. M1: recorded fixtures, `make demo` with `demo/golden_output.txt`, the three examples;
   then tag v0.1. After that, M2: port prereg, claimcheck, freshness, and mutation tests
   from raise-v1-public (needs `add_repo` read access to that public repository).

## Environment (make doctor, 2026-10-23)

- Linux x86_64, 4 CPUs, 15.7 GB RAM, no GPU, 29.9 GB free disk (floor 8 GB).
- Python 3.11 and 3.12 present; uv 0.11.32. Network reaches PyPI, Hugging Face, ollama.com, arXiv.
- Docker: client only, no daemon. Ollama, llama.cpp, torch, transformers: not installed.
- Sandbox: `unshare -rn` (network isolation only); no bubblewrap or Docker (DECISIONS.md D3).
- Backends: local open-weight models installable, not installed yet. Claude plan
  **unusable** (billing guard: `ANTHROPIC_BASE_URL` and `CLAUDE_CODE_USE_CCR_V2` are set,
  DECISIONS.md D2). Free tiers: none configured. Review: `reviewer` subagent fallback.
- `ALLOW_PUSH` not set; pushes go to the session branch only (DECISIONS.md D1).

## Usage to date

No model calls. Ledger empty. cash_usd = 0.

## Needs Raj

None of these blocks progress now; the Definition of Done is reachable without them.

1. **Confirm pushing.** This cloud session pushes commits to
   `claude/tender-ptolemy-ilsn9p` only (never main, tags, or force; DECISIONS.md D1).
   Set `ALLOW_PUSH=1` in the environment to make that explicit, or say stop.
2. **Claude arms (optional).** Claude subject runs, Claude-judge monitors, and headless
   `make review` need a machine where `claude` is logged in to the Max plan and none of
   `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, or `CLAUDE_CODE_USE_*`
   is set. Without it the study fields four families without Claude (docs/PLAN.md).
3. **Sandbox (optional).** A running Docker daemon or bubblewrap would allow third-party
   code; until then only this project's own tasks with in-process mock tools run.

## Blocked

Nothing.

## Backlog (suggestions for Raj, not ladder rungs)

- A machine-readable monitor report card (`baserate card --json`) that states the
  certification gap in words a safety case can quote ("N more independent benign runs
  are needed to claim a false-positive rate of at most f"), so a lab can attach it to a
  monitoring section as is.
