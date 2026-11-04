---
id: 0005
title: E4 cascade optimization under compute and review budgets, validated on held-out data
experiment: E4
date: 2026-11-04
---

# E4: cost-recall fronts of cheap-to-expensive cascades (confirmatory)

## Question (RQ4)

Under a fixed human-review budget, how much monitor compute does a two-stage
cascade save relative to running the expensive monitor on everything, and
does a cascade chosen on one half of the data keep its recall on the other?

## Data, cascades, and costs

The `primary` set of the E1 corpus with the scores banked by E2 (no new
scoring). Cascade pairs (stage 1 then stage 2) are listed in the config. The
cost of a monitor is its median wall-clock seconds per scored trace in the
E2 scoring runs (recorded per trace), measured once from the banked score
files; rule monitors are given the median of their recorded time per trace.
Costs are a measured proxy of compute on this machine, not a price.

## Method

- Split: source runs are assigned to training and test halves with
  `baserate.cascade.split_by_run` (stratified by whether a run has a
  violation trace; seed in the config), so every trace of a run is on one
  side.
- Selection on training data: `baserate.cascade.sweep_cascade` over the
  default grid of escalation rates at the stated prevalence and budget; the
  chosen cascade is the highest-recall point with expected cost at most
  half the cost of running stage 2 on all traffic.
- Evaluation on test data: `baserate.cascade.validate_holdout`, which
  applies the chosen escalation rule to the test half and gives a test
  recall interval from a cluster bootstrap that repeats the selection on
  every resampled training set. Stage 2 alone on the test half is evaluated
  with `recall_at_budget` and the E2 headline method.
- The full cost-recall Pareto front on the whole primary set is reported for
  every pair (descriptive).

## Hypotheses and decision rules (one per cascade pair)

- H4.1 (validation): the training estimate of the chosen cascade's recall
  lies inside its 95% test-recall interval. Supported if it does for the
  pair; not supported otherwise. (There is no inconclusive outcome.)
- H4.2 (efficiency): with at most half of stage 2's compute, the chosen
  cascade's test recall is no more than 0.05 below the test recall of stage
  2 alone. Supported if the cascade's test-recall point estimate is at least
  the stage-2-alone point estimate minus 0.05; not supported if the upper
  end of the cascade's 95% test-recall interval is below the stage-2-alone
  point estimate minus 0.05; inconclusive otherwise.

Each hypothesis id is suffixed with the pair index (for example H4.2-p1).
Every outcome is reported in docs/REPORT.md.

```json prereg-config
{
  "experiment": "E4",
  "corpus_prereg": "prereg/0002-e1-corpus.md",
  "scores_from": "prereg/0003-e2-main-study.md",
  "set": "primary",
  "pairs": [
    {"index": "p1", "stage1": "model:qwen3-0.6b-q8_0", "stage2": "model:qwen3-8b-q4_k_m"},
    {"index": "p2", "stage1": "rule:armour-reviewer-calibrated", "stage2": "model:qwen3-8b-q4_k_m"},
    {"index": "p3", "stage1": "model:qwen3-0.6b-q8_0", "stage2": "model:qwen3-4b-q4_k_m"},
    {"index": "p4", "stage1": "model:qwen3-1.7b-q8_0", "stage2": "model:qwen3-8b-q4_k_m"}
  ],
  "prevalence": 0.001,
  "budget_per_10k": 20,
  "compute_budget_fraction_of_stage2": 0.5,
  "non_inferiority_margin": 0.05,
  "test_fraction": 0.5,
  "split_seed": 20261106,
  "alpha": 0.05,
  "resamples": 1000,
  "seed": 20261107,
  "hypotheses": ["H4.1", "H4.2"]
}
```
