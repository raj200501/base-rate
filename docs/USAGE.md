# Using base-rate on your own benchmark or agent

This page shows how to evaluate a monitor of your own with base-rate: what
data you need, the score-file format, the command-line tools, and how to read
what they print. It assumes you can run your monitor over a labeled sample of
agent trajectories and save one score per trajectory.

## What you need

1. **A labeled sample.** Benign trajectories and violation trajectories,
   each labeled by people or by a trusted oracle, never by the monitor under
   test. The two classes may be sampled separately (a case-control design):
   base-rate reweights them to the prevalence you state.
2. **One score per trajectory** from your monitor, where a higher score means
   more suspicious. Rule monitors that output 0 or 1, coarse scores with many
   ties, and continuous probabilities all work.
3. **The independent source run of each trajectory** (see
   [Clusters](#clusters-counting-independent-runs)). This is the most
   important input after the labels: it decides how much evidence the sample
   holds.
4. **Your deployment assumptions:** the prevalence of violations (for example
   1 in 10,000 trajectories) and the review budget (for example humans
   review 20 of every 10,000 trajectories). These are inputs, not estimates.

## Install

From a checkout of the repository:

```
pip install -e .        # or: uv sync
baserate --help
```

The only runtime dependencies are NumPy and SciPy. Nothing in this page needs
the network or a model.

## The score file

A score file has one row per labeled trajectory. CSV files need a header row;
JSON Lines files (`.jsonl` or `.ndjson`) hold one JSON object per line with
the same keys.

| Column | Needed | Meaning |
|---|---|---|
| `trace_id` | yes | Unique id of the trajectory. |
| `label` | yes | `benign` or `violation`. |
| `score` | yes | The monitor's score, a finite number; higher is more suspicious. |
| `cluster` | yes | Id of the independent source run the trajectory descends from. |
| `stage2_score` | no | A second, more expensive monitor's score, for `baserate cascade`. Leave it empty (CSV) or `null` (JSON Lines) where stage 2 did not score the trajectory. |
| `weight` | no | A positive sampling weight (the inverse of the inclusion probability) when you sampled some strata more heavily than others. If the column exists, every row needs a value. |
| `synthetic` | no | `true` or `false`: whether the trajectory is synthetic or procedurally generated. The report card states the synthetic share when this column exists. |

A small CSV file with made-up values:

```
# Scores of my-monitor v3 on the March benign sample and the red-team set.
trace_id,label,score,cluster,synthetic
run-0001,benign,0.12,run-0001,false
run-0001-v1,benign,0.18,run-0001,true
run-0002,benign,0.05,run-0002,false
run-0002-ins,violation,0.91,run-0002,false
```

The same rows as JSON Lines:

```
{"trace_id": "run-0001", "label": "benign", "score": 0.12, "cluster": "run-0001", "synthetic": false}
{"trace_id": "run-0002-ins", "label": "violation", "score": 0.91, "cluster": "run-0002", "synthetic": false}
```

Details of the format:

- Column names are matched after trimming spaces and lowercasing, so
  ` Trace_ID ` works. Other columns are allowed and ignored.
- Lines whose first non-space character is `#` are comments. Their text is
  kept and printed on the report card, so a file can say where its data came
  from. Use them for that.
- Blank lines are skipped. A UTF-8 byte-order mark is accepted.
- The file needs at least one benign and one violation row, and every
  `trace_id` must be unique.

The first problem found is reported with the file name and, for a bad row,
its line number, for example `scores.csv:14: label must be 'benign' or
'violation', got 'safe'`, and the command exits with code 1.

To write a score file from Python, build a `baserate.scores.ScoreSet` and
call `baserate.scores.write_scores(scores, "scores.csv")`; the floats are
written so that reading the file back gives identical values.

## Clusters: counting independent runs

Trajectories that descend from one agent run share most of what a monitor
sees: the task, the tools, the agent's habits, and often whole steps. If they
were counted as independent evidence, every interval would be too narrow. The
`cluster` column says which trajectories belong together, and the default
interval methods count clusters, not trajectories, as the independent units.

What to put in `cluster`:

- **A trajectory that is its own independent run:** its own id (the
  `trace_id` works).
- **Procedural variants** (paraphrased, reordered, or otherwise varied copies
  of one run): the source run's id, for every variant.
- **Inserted violations** (a benign run with one violating step inserted and
  the rest regenerated): the benign run's id. A cluster can hold both benign
  and violation trajectories; the counts are kept per label.
- **Several samples from one deterministic generator** (scripted agents,
  templated fixtures): one id for the generator and its configuration. Records
  from the same script are not independent even when each has its own id.
- **Retries and resumed sessions of one task:** one id.

When in doubt, use the coarser grouping. Too few clusters makes intervals
wider than they need to be; too many makes them narrower than the data
support, which is the error that matters.

The number of independent benign clusters limits how small a false-positive
rate you can claim for a monitor's own decision rule. With no false positive
at all among `n` independent benign runs,
the one-sided 95% upper bound on the false-positive rate is about `3 / n`, so
no smaller rate can be claimed from them, however many variants each run has.
The report card states this floor for your data.

## The commands

Every command prints `--help`. Exit codes are 0 on success, 2 on a usage
error (a missing or invalid argument), and 1 on a data error (a missing or
invalid score file, or data a computation cannot use).

### `baserate eval`

```
baserate eval SCORES --prevalence P [P ...] --budget B [B ...] \
    [--method composite] [--alpha 0.05] [--reps 1000] [--seed 0] [--json]
```

For each prevalence and review budget (reviews per 10,000 trajectories), the
monitor's top-scoring share of traffic is reviewed, and `eval` prints recall
(the share of violations reviewed), precision (the share of reviews that are
violations), and the false-positive rate (the share of benign traffic
reviewed), each with a two-sided interval. Beside each row are the trace and
cluster counts and any E0 support warning (see
[What the intervals mean](#what-the-intervals-mean)). `--json` prints the
same numbers at full precision.

### `baserate certify`

```
baserate certify --fpr F [--recall R] [--alpha 0.05] [--power 0.8] \
    [--true-fpr F0] [--true-recall R0] \
    [--have-runs N --have-events K] [--have-violation-runs N --have-misses K] [--json]
```

How many independent runs a claim needs. A claim "FPR is at most F" or
"recall is at least R" is certified when the exact one-sided bound at
confidence `1 - alpha` meets it. `certify` prints:

- the **zero-event** count: the runs that certify the claim if none of them
  has a false positive (or a miss);
- the **power-based** count: the runs from which the chance of certifying
  stays at or above `--power`, if the true rate is `--true-fpr` (default
  `F / 2`) or `--true-recall` (default `(1 + R) / 2`);
- with `--recall`, both claims together at one overall confidence;
- with `--have-runs N --have-events K` (benign runs so far and how many had
  a false positive), how many **more** runs the claim needs: if none of them
  has a false positive, if false positives continue at the observed rate,
  and for the target power. `--have-violation-runs` and `--have-misses` do
  the same for recall.

`certify` reads no data file; it only needs the counts. Count runs, not
trajectories: a benign run is a false positive if any of its trajectories is
flagged, and a violation run is detected only if all of its trajectories are.

### `baserate cascade`

```
baserate cascade SCORES --prevalence P --budget B --c1 C1 --c2 C2 \
    [--max-cost C] [--num-rates 25] [--all] [--json]
```

A two-stage cascade: a cheap stage-1 monitor (`score`) scores every
trajectory, the top share by stage-1 score is escalated to an expensive
stage-2 monitor (`stage2_score`), and humans review the top of the escalated
trajectories by stage-2 score, up to the budget. The expected cost per
trajectory is `C1 + e * C2` for an escalated share `e`. `cascade` sweeps `e`
from the flag rate to 1, prints the cost-recall Pareto front (the points no
other point beats on both cost and recall), and with `--max-cost` the
highest-recall point within that cost.

Stage 2 only needs scores for trajectories a swept rule escalates; an
escalated trajectory with no stage-2 score is a data error. The sweep gives
point estimates on the file's trajectories, without intervals, and a rule
picked from it is picked on the same data. Before relying on a rule, choose
it on one split of source runs and test it on another with
`baserate.cascade.split_by_run` and `baserate.cascade.validate_holdout`,
which also give a cluster bootstrap interval for the held-out recall. Weighted
score files are not supported by the sweep.

### `baserate card`

```
baserate card SCORES --name NAME [--threshold T] [--note TEXT] \
    [--method composite] [--alpha 0.05] [--json] [--out PATH]
```

A monitor report card in Markdown (or JSON with `--json`): every number on it
is computed from the score file. `--threshold` is the monitor's own decision
rule (flag when the score is at least `T`); without it the card leaves out the
decision-threshold section and states the certification gap as a lower bound.
`--note` adds a line to the card's notes, for example where the data came
from. `--out` writes the card to a file instead of printing it.

### `baserate demo`

Runs the offline demo on recorded Armour fixtures and prints its output. It
needs no network and makes no model calls.

## Reading the card

**Data.** Trajectories and independent clusters per label, the synthetic
share when the file has a `synthetic` column, whether sampling weights were
used, and the false-positive floor: the smallest false-positive rate the
benign clusters could certify even with no false positive at all. Comment
lines of the score file are repeated here.

**Recall and precision at a review budget.** One row per prevalence (1e-2,
1e-3, 1e-4) and budget (5, 20, and 100 per 10,000). Read a row as: if
violations are this common and humans review this many trajectories out of
10,000, chosen as the monitor's top scores, this share of violations is
caught (recall), this share of reviews are violations (precision), and this
share of benign traffic is reviewed (FPR). Two limits follow from the
budget alone. Write `b = B / 10,000` for the reviewed share. When `b` is below
the prevalence, recall is at most `b / prevalence`, because there are more
violations than reviews. Precision is at most `prevalence / b`, so at a
budget far above the prevalence most reviews are of benign trajectories
however good the monitor is.
The last column shows whether the interval method under-covered in a similar
configuration of experiment E0, and the line under the table says where the
E0 data come from.

**Decision threshold.** When `--threshold` is given: the false-positive rate
and recall when trajectories scoring at or above it are flagged, with exact
bounds that count clusters (`cluster_cp`), the number of benign clusters with
a flagged trajectory, the one-sided upper bound on the false-positive rate
that counts those clusters, and precision at each prevalence.

**Certification gap.** For each budget `B`, the false-positive rate that fits
it, `B / 10,000`, and in plain words how many more independent benign runs
with no false positive are needed to claim it at the card's confidence. When
false positives already occurred at the observed rate, the card also says
whether adding runs at that rate could ever reach the claim.

**Notes.** Sections that were left out and why, your `--note` lines, and the
condition under which the numbers carry over to a deployment.

## What the intervals mean

Every interval is a two-sided `1 - alpha` interval (95% by default) unless it
is marked one-sided. With the default methods every interval and bound counts
clusters as the independent units; the exceptions are the `plugin_*` methods
described below, which `eval` and the card only use when you ask for them.

- **At a review budget** (`eval` and the card's budget table) the default
  method is `composite`. The threshold that meets the budget is itself
  uncertain, because it is set by the benign tail, so `composite` resamples
  benign clusters to capture that, and combines it with exact
  Clopper-Pearson bounds on the violation clusters. Each of its four tails
  spends a quarter of `alpha`. Precision and the false-positive rate at an
  exact budget are functions of recall, so their intervals follow from the
  recall interval. `--method` selects one of the other methods in
  `baserate.estimate.BUDGET_METHODS`; the docstring of
  `baserate.estimate.atbudget` describes each. `cluster_cp`,
  `cluster_bootstrap`, and `bayesian_bootstrap` also count clusters. The
  `plugin_cp`, `plugin_wilson`, and `plugin_jeffreys` methods do not: they
  are binomial intervals on the violation trajectories at the estimated
  threshold, so they treat trajectories as independent and ignore the
  threshold's own uncertainty. They are there for comparison, and the output
  says so when one is used.
- **At a fixed threshold** the card uses `cluster_cp`: the upper bound on a
  rate counts the clusters with any flagged trajectory and the lower bound the
  clusters with every trajectory flagged, each with an exact Clopper-Pearson
  bound at `alpha / 2`.
- **Certification** uses exact one-sided bounds at `alpha`, the same function
  for planning and for checking, so a plan and the later analysis cannot
  disagree.
- **E0 support.** Experiment E0 measures, by simulation, how often each
  method's intervals cover the true value across prevalences, budgets,
  sample sizes, and score distributions. A configuration where a method
  under-covered is listed in the package, and `eval` and the card flag any
  row that matches one. The card states where the E0 data come from; until E0
  has run, no row can be flagged, which is not evidence that the methods
  cover.

What the intervals do not cover: label errors, a sample that does not
represent deployment traffic, a different monitor version or prompt, and a
wrong prevalence. Prevalence and budget are inputs; state them as
assumptions when you report a number.

## From Python

The command-line tools are thin layers over the library:

```python
import numpy as np

from baserate.card import build_card, render_markdown
from baserate.certify import more_runs_fpr
from baserate.estimate import flag_rate_from_budget, recall_at_budget
from baserate.scores import read_scores

scores = read_scores("scores.csv")
est = recall_at_budget(
    scores.neg, scores.pos, 1e-3, flag_rate_from_budget(20),
    neg_clusters=scores.neg_clusters, pos_clusters=scores.pos_clusters,
    neg_weights=scores.neg_weights, pos_weights=scores.pos_weights,
    rng=np.random.default_rng(0),
)
print(est.recall, est.precision, est.n_neg_clusters, est.warnings)

card = build_card(scores, "my-monitor v3", threshold=0.5)
print(render_markdown(card))

print(more_runs_fpr(false_positives=0, runs=scores.n_neg_clusters, max_fpr=1e-3))
```

`examples/` has three runnable scripts that do this on recorded fixtures:
`examples/evaluate_your_monitor.py`, `examples/certify.py`, and
`examples/build_cascade.py` (see `examples/README.md`).

## Limitations

- base-rate assumes the benign sample represents benign deployment traffic
  and the violation sample represents the violations you care about. It
  cannot check either. Report how each sample was drawn.
- Violations from different sources (natural, inserted, public datasets) can
  differ in how easy they are to catch; evaluate them separately when you
  have them.
- Sampling weights are used by the budget estimates only. The fixed-threshold
  bounds, the certification gap, and the cascade sweep assume an unweighted
  sample of independent runs.
- The cascade sweep reports in-sample point estimates.
- Nothing here detects a monitor that was tuned on the evaluation sample;
  keep the sample out of monitor development, or say that it was not.
