# Armour fixtures

Verbatim copies of files from Armour, https://github.com/raj200501/armour, at
commit `639eb62ae17a8c3876137f375176417ff6671ea0`, copied on 2026-10-23. Armour
is by the author of this repository and is released under the MIT License; its
license text is in `LICENSE` here, as the license requires for copies.

Each file keeps its path from the Armour repository root, so this directory
can stand in for an Armour checkout (`load_armour_datasets(<this directory>)`),
and the line numbers recorded in trace provenance match the originals.

| File | SHA-256 | Used for |
|---|---|---|
| `LICENSE` | `06c6a6db980e60ed778ebf27a14bfe218a87872e6ad8244ba3f4a1a528bfb34b` | Armour's MIT license |
| `armour_labs/data/evals.json` | `37db43d8edd58d30f376dd7b9dfa1c12abef81b16760db035d40824926438c56` | The 12 seed eval cases, checked against the ported catalog |
| `examples/traces/risky_customer_ticket.json` | `c7919c8fa497dd1906ec7b41bfb6e30284bf27f18419fa16cceb2ddbf5c76515` | Armour's example `AgentTrace` |
| `examples/agent_logs/mcp_customer_ticket.jsonl` | `5c94f9438bfcfc21bcd66ecf4df957760a32381b3d4db61b407c7766b4326eaf` | Armour's example MCP event log |
| `datasets/local_agent_labeled_traces.jsonl` | `c95a25dde3e664910d0977c1073d20e33286b7cb5ef7345d9955585c3d8ebf8e` | Labeled records (dataset loader) |
| `datasets/live_external_agent_runs.jsonl` | `e08d67d0bc80f40f6e51ed1b0db59c5c58b3e89153c8a386e03765de90303f03` | Labeled records; traces for the adjudication queue |
| `datasets/reviewed_external_agent_traces.jsonl` | `be4a48e46b53fe400a5b688009308e667fbbaff20d20404eeaa9a1ebef587463` | Labeled records with reviews |
| `datasets/model_agent_runs.jsonl` | `477d9c87eac422e01c4ee2f5203f6ffbece326b83b24effe52fea1510a7b858b` | Model-generated records |
| `datasets/model_agent_claim_candidates.jsonl` | `825d5f166e47c63f7203177a5cc7962e93671b5d9f4201e76e068e31b61bda03` | Traces for the outcome-state report (a subset of `model_agent_runs.jsonl`) |
| `datasets/adjudication_queue_v3.jsonl` | `b5c02abc7fbf9ac27b4087b8b47c28731cb918e870d0f6fd6dd1155f33649159` | Recorded output (see below); has no event logs |
| `benchmarks/outcome_state_model_claim_candidates.json` | `a86e0cebc9c81176bbd01c2571d95d18b59da030369cb4e0362d8ad28cefbbf2` | Recorded output (see below) |

## Recorded Armour outputs

No Armour code was run to make these fixtures (DECISIONS.md D3 bars running
third-party code on this host). The rule-monitor tests compare the port with
outputs that Armour's own scripts produced and committed at that commit:

- `benchmarks/outcome_state_model_claim_candidates.json`, written by Armour's
  `scripts/generate_outcome_state_report.py` with the `reviewer-calibrated`
  pack: every finding (step, rule id, severity) for each of the 21 records in
  `datasets/model_agent_claim_candidates.jsonl`.
- `datasets/adjudication_queue_v3.jsonl`, written by Armour's
  `scripts/generate_benchmark_v3.py` with the default (`baseline`) pack: the
  verdict and fired rule ids for the 16 records of
  `datasets/live_external_agent_runs.jsonl` where the monitor disagreed with
  the label.
- Armour's `tests/test_replay.py` asserts that on
  `datasets/live_external_agent_runs.jsonl` the `baseline` pack has accuracy
  0.5 and `reviewer-calibrated` has accuracy 1.0 with no false positives or
  false negatives. Armour's `tests/test_monitors.py`,
  `tests/test_policy_packs.py`, and `tests/test_adapters.py` assert rule ids
  and verdicts on small traces; the base-rate tests repeat those checks.

Armour commits no scan output for the two files under `examples/`; the
expected rule ids for them in `tests/test_rule_monitors.py` were derived by
reading Armour's rules and are marked as such.

## Content

All traces are abstract actions in mock tools (for example a mock
`bulk_export` call on a `customers` table). URLs, account ids, and tokens in
them are placeholders, not real systems or credentials.
