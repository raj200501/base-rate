---
id: 0001
title: E0 statistical engine validity
experiment: E0
date: 2026-10-23
---

# E0: coverage of every interval method in baserate.estimate

## Purpose

Validate, by simulation and before any monitor data exists, the coverage of
every interval method the library offers for (a) recall at a fixed review
budget and deployment prevalence and (b) the false-positive rate, recall, and
precision at a fixed decision threshold. The result decides which methods
experiment E2 may use for headline intervals, and which configurations the
library warns about.

## Design

Each cell of the grid below combines a score distribution, a sample-size
design, a deployment prevalence, and a review budget. For each cell the exact
population truth is computed by `baserate.estimate.simulate.truth` (root
finding on exact survival functions for continuous models; exact level
probabilities with randomized ties for the discrete model). Replicate datasets
are drawn by `baserate.estimate.simulate.sample`; benign traces in the
clustered design share a latent cluster effect (Gaussian copula,
intra-cluster correlation 0.5 on the latent normal scale), which leaves the
marginal benign distribution unchanged. Every estimator is run on the same
replicate datasets. Fixed-threshold estimators use the cell's exact
population budget threshold as the fixed threshold.

- Score distributions: `binormal_d1` (benign N(0,1), violation N(1,1)),
  `binormal_d3` (N(0,1) vs N(3,1)), `heavy_tail` (benign Student t with 3
  degrees of freedom, violation N(5,1)), `discrete4` (latent N(0,1) vs N(2,1)
  cut at 0.5, 1.5, 2.5 into four tied levels, like a rule monitor).
- Sample-size designs: `n2000_p100` (2,000 independent benign, 100 violations),
  `n20000_p500` (20,000 independent benign, 500 violations), `n20000c10_p500`
  (20,000 benign in 2,000 source-run clusters of 10, intra-cluster correlation
  0.5, and 500 independent violations).
- Prevalences 1e-2, 1e-3, 1e-4. Review budgets 5, 20, and 100 per 10,000.
- Estimators: the 7 budget-recall methods of `recall_at_budget` and the 5
  fixed-threshold interval methods applied to FPR, recall, and precision (22
  estimators; listed in the config block).
- Replicates: 2,000 per cell; 500 per cell for estimators that refit by
  resampling (`cluster_bootstrap`, `bayesian_bootstrap`, `composite`), run on
  the first 500 of the same replicate datasets. Resamples per interval: 1,000.
- Seeds: the confirmatory run uses seed 20261023 with
  `numpy.random.SeedSequence(seed, spawn_key=(cell_index,))` per cell. The dry
  run (harness check only) uses seed 999 with 20 replicates and writes only
  under runs/e0-dry/; it is never pooled with or reported as confirmatory data.

## Criterion (frozen)

For each estimator and cell, with coverage c over R replicates and Monte Carlo
standard error s = sqrt(c(1 - c)/R), the cell passes if
c >= 0.95 - 0.015 - 2s. Over-coverage is reported, not failed. Mean interval
width is reported alongside.

## Analysis and reporting

- Every cell is run to completion; there is no early stopping and no rerun
  with another seed. A crash is resumed with the same per-cell seeds.
- No exclusions.
- Failing cells are written to `src/baserate/estimate/unsupported_cells.json`
  (the library warns on matching calls) and to docs/UNSUPPORTED.md. Coverage
  plots per metric family are banked under artifacts/e0/plots/.
- Headline rule for E2: an E2 headline interval relies on every E0 cell with
  the same estimator, prevalence, and review budget, and the sample-size
  design whose cluster structure matches and whose sizes are closest on a log
  scale, across all four score distributions. E2 may use a method for a
  headline interval only if all of those cells pass. E2's artifact lists the
  E0 cell ids each headline interval relies on.
- Code: the estimators and simulation code as committed with this file
  (`src/baserate/estimate/`), and the driver `tools/run_e0.py`.

```json prereg-config
{
  "experiment": "E0",
  "nominal": 0.95,
  "tolerance": 0.015,
  "alpha": 0.05,
  "criterion": "coverage >= nominal - tolerance - 2 * sqrt(coverage * (1 - coverage) / replicates)",
  "grid": {
    "estimators": [
      "budget_recall:plugin_cp",
      "budget_recall:plugin_wilson",
      "budget_recall:plugin_jeffreys",
      "budget_recall:cluster_cp",
      "budget_recall:cluster_bootstrap",
      "budget_recall:bayesian_bootstrap",
      "budget_recall:composite",
      "fixed_fpr:cp",
      "fixed_fpr:wilson",
      "fixed_fpr:jeffreys",
      "fixed_fpr:cluster_cp",
      "fixed_fpr:cluster_bootstrap",
      "fixed_recall:cp",
      "fixed_recall:wilson",
      "fixed_recall:jeffreys",
      "fixed_recall:cluster_cp",
      "fixed_recall:cluster_bootstrap",
      "fixed_precision:cp",
      "fixed_precision:wilson",
      "fixed_precision:jeffreys",
      "fixed_precision:cluster_cp",
      "fixed_precision:cluster_bootstrap"
    ],
    "prevalences": [0.01, 0.001, 0.0001],
    "budgets_per_10k": [5, 20, 100],
    "sample_sizes": ["n2000_p100", "n20000_p500", "n20000c10_p500"],
    "score_distributions": ["binormal_d1", "binormal_d3", "heavy_tail", "discrete4"]
  },
  "designs": {
    "n2000_p100": {"neg_clusters": 2000, "neg_cluster_size": 1, "n_pos": 100, "icc": 0.0},
    "n20000_p500": {"neg_clusters": 20000, "neg_cluster_size": 1, "n_pos": 500, "icc": 0.0},
    "n20000c10_p500": {"neg_clusters": 2000, "neg_cluster_size": 10, "n_pos": 500, "icc": 0.5}
  },
  "models": {
    "binormal_d1": "benign N(0,1); violation N(1,1)",
    "binormal_d3": "benign N(0,1); violation N(3,1)",
    "heavy_tail": "benign t(df=3); violation N(5,1)",
    "discrete4": "latent benign N(0,1), violation N(2,1); cut at 0.5, 1.5, 2.5; scores level/3"
  },
  "replicates": 2000,
  "bootstrap_replicates": 500,
  "resamples": 1000,
  "seed": 20261023,
  "dry_run_seed": 999,
  "dry_run_replicates": 20
}
```
