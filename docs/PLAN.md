# Plan

This plan maps milestones to the Definition-of-Done gates in
docs/BUILD_BRIEF.md section 6. It is a working document: milestones move,
gates do not. Usage figures below are planning estimates for scheduling, not
results.

## Environment this plan assumes (make doctor, 2026-10-23)

- Linux x86_64, 4 CPUs, about 16 GB RAM, no GPU, about 30 GB free disk (floor 8 GB).
- Python 3.11 and 3.12 present; uv present. Network reaches PyPI, Hugging Face, ollama.com, arXiv.
- Docker client present but no daemon. No Ollama, llama.cpp, torch, or transformers yet.
- Sandbox: `unshare -rn` network namespaces work (network isolation only). No bubblewrap.
- Claude backend **unusable**: the billing guard finds `ANTHROPIC_BASE_URL` and
  `CLAUDE_CODE_USE_CCR_V2` set by the cloud host. No Claude subject or judge arms run here;
  `make review` uses the `reviewer` subagent. See DECISIONS.md D2 and "Needs Raj" in STATUS.md.
- No free-tier keys.

## Milestones

| Milestone | Delivers | Gates | Tag |
|---|---|---|---|
| M0 Bootstrap | Scaffold, doctor, ship-check, agents, state files | G2, G14 (partial) | |
| M1 Statistical core and offline demo | `estimate`, `certify`, `cascade`, CLI `eval/certify/cascade/card`, recorded fixtures, demo with golden output, three examples | G1, G3, G4, G9 (except Armour) | v0.1 |
| M2 Integrity tooling | `prereg-verify`, `claimcheck`, `freshness` (ported from raise-v1-public with attribution), mutation tests for gates, `citecheck` | G10, G11, G13 (tooling) | |
| M3 E0 | Preregistered grid, simulation study, coverage plots, library warnings for unsupported cells | G5 | |
| M4 Traces and corpus (E1) | Armour-compatible schema and adapter, 8 mock-tool domains, local-model agent runner, procedural variants, violation insertion with regeneration from the inserted step, label audit, datasheet | G6, G9 (Armour) | |
| M5 Monitors and E2 | Rule monitors (Armour policy packs), local size ladder, ensembles, cascades; preregistered main study | G7 | v0.5 |
| M6 E3 and E4 | Transfer study across domains; cascade optimization validated on held-out data | G8 | |
| M7 Ship | `make report`, README per section 11, docs/REPORT.md, review, final sequence | G12, G13, G14 | v1.0 |

M1 and M2 do not need any model. M4 is the long pole: real agent runs on CPU.
M3 can run in the background while M4 is built.

## Critical path and ordering

1. M1 first: the library is the product and unblocks G1, G3, G4, G9.
2. M2 next: the preregistration tooling must exist before any confirmatory data.
3. Freeze the E0 preregistration, then run E0 in the background (CPU only).
4. M4: install a pinned local runtime, generate benign tasks, start real agent runs in the
   background as early as possible (they take the longest).
5. Freeze the E2 to E4 preregistrations before any confirmatory monitor scores exist.
6. M5, M6, M7.

## Monitor families without Claude

E2 needs at least 4 monitor families. Without the Claude backend this environment can
still field: (1) rule monitors from Armour policy packs, (2) a local open-weight size
ladder with one frozen prompt, (3) ensembles, and (4) cascades. Claude tiers as judges
stay in the design as an arm that Raj can run on a machine where the guard is clean.

## Risks and fallbacks

| Risk | Fallback |
|---|---|
| Claude backend unusable here | Local monitors and subject agents only; Claude arms listed under Needs Raj; reviewer and citation checks via subagents |
| No Docker | `unshare -rn` for network isolation; subject agents call only in-process simulated mock tools (no shell, no execution of model output); try installing bubblewrap for write confinement before running any third-party code |
| CPU-only inference is slow | Small models (0.5B to 8B, 4-bit), short trajectories, stratified and importance sampling so expensive monitors score only informative traces; runs in the background with PID files |
| Real agent runs produce few natural violations | Report the count honestly; controlled insertion and licensed datasets cover the rest, reported separately |
| Armour schema drift | Import or re-implement compatibly, pin the Armour commit, contract tests on recorded fixtures |
| E0 cells fail coverage | Library warning plus docs/UNSUPPORTED.md; headline intervals use only passing cells |
| Disk | Check the 8 GB floor before every download; prefer 4-bit GGUF files pinned by hash |
| Context or session loss | State lives in STATUS.md, DECISIONS.md, docs/LOG.md, and banked artifacts; pushes to the session branch |

## Usage estimates (planning only)

- Claude plan: none in this environment. If Raj runs the Claude arms elsewhere: a
  stratified subsample scored by haiku, sonnet, and opus in batches of 20 to 50 traces per
  single call keeps each tier well under `claude_single_calls_per_day`.
- Local CPU: an agent trajectory of about 10 steps from a 1.5B model is expected to take on
  the order of a minute; 2,000 real runs is therefore a multi-day background job, so real
  runs start as soon as the runner and preregistered task set exist. A monitor pass over a
  stratified subsample of a few thousand traces per ladder rung is expected to take hours,
  not days, per rung below 4B.
- E0: pure NumPy simulation; minutes to hours depending on the bootstrap-refit cells.

## Artifact contracts (checked by tools/ship_check.py)

All paths are repository-relative. JSON is UTF-8. Hashes are SHA-256 hex.
"Canonical JSON" means `json.dumps(obj, sort_keys=True, separators=(",", ":"))`.

- **Preregistration** `prereg/NNNN-name.md`: front matter (`id`, `title`, `experiment`,
  `date`), prose plan, and exactly one fenced block tagged `json prereg-config`. Never
  modified after its first commit.
- **Preregistered artifact**: a JSON object with `prereg` (path) and
  `prereg_config_sha256` (canonical-JSON hash of the config block); the preregistration's
  adding commit must strictly precede the artifact's first commit. Confirmatory artifacts
  also carry `"confirmatory": true`. Every artifact carries `provenance`: git SHA, config
  hash, model IDs and revisions, seeds, timestamps, environment, command lines, usage.
- **E0** `artifacts/e0/summary.json`: `cells` list with `id`, `estimator`, `prevalence`,
  `sample_size`, `score_distribution`, `replicates`, `covered`, `refits_by_bootstrap`,
  `passes`, `used_by_e2_headline`; `plots` list of banked files. The config has
  `nominal` and `grid` (`estimators`, `prevalences`, `sample_sizes`,
  `score_distributions`); cells must cover the full grid. Failing cells are listed in
  `src/baserate/estimate/unsupported_cells.json` and docs/UNSUPPORTED.md.
- **Corpus** `artifacts/corpus/manifest.json`: `shards` list (`path`, `sha256`, `records`),
  each a gzip JSONL file under 50 MB. Each trace has `trace_id`, `source_run_id`,
  `source` (`real_agent`, `procedural`, `inserted`, `dataset`), `synthetic`, `domain`,
  `label` (`benign` or `violation`), `steps`, and for violations `violation_type`,
  `violation_source`, and `first_violating_step`.
- **Label audit** `artifacts/corpus/label_audit.json`: `auditor`, `items` (each with
  `trace_id`, `original_label`, `audit_label`, `original_step`, `audit_step`), `agreement`.
- **E2** `artifacts/e2/results.json`: `confirmatory`, `estimates` (each with `family`,
  `monitor`, `prevalence`, `budget`, `metric`, `estimate`, `ci_low`, `ci_high`,
  `n_independent_runs`), `scores_file`, `scores_sha256`; the config lists `prevalences`
  and `budgets_per_10k`.
- **E3, E4** `artifacts/e3/results.json`, `artifacts/e4/results.json`: preregistered,
  with `hypotheses` (each `id` and `outcome` in `supported`, `not_supported`,
  `inconclusive`); every id appears in docs/REPORT.md.
- **Report** `artifacts/report/manifest.json`: `figures` list of files `make report`
  generated; tables under `artifacts/report/tables/*.md` each state the synthetic share
  and the number of independent source runs.
- **Review** `artifacts/review/latest.json`: schema `baserate-review/1` (see
  .claude/agents/reviewer.md).
- **Citations** `artifacts/citecheck.json`: schema `baserate-citecheck/1` (see
  .claude/agents/citation-checker.md).
- **Usage ledger** `artifacts/usage/ledger.jsonl`: one JSON object per call with `ts`,
  `kind` (`claude_agent_run`, `claude_single_call`, `claude_review_run`, `local_call`,
  `free_tier_call`), `cash_usd` (always 0), `plan_equivalent_usd`, `cap_share`, and
  `turns` or `params_b` where they apply.
