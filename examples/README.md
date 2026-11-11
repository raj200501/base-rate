# Examples

Three short scripts show the main uses of base-rate. Each runs offline in a
few seconds, makes no model calls, and reads only the recorded fixtures in
`examples/fixtures/` (Armour mock-tool traces scored by Armour rule packs;
demonstration data, not study results).

```
pip install -e .                      # from the repository root
python examples/evaluate_your_monitor.py
python examples/certify.py
python examples/build_cascade.py
```

With uv, `uv run python examples/<name>.py` works the same way.

| Script | Question it answers | Library calls |
|---|---|---|
| `examples/evaluate_your_monitor.py` | If humans review a fixed share of traffic, what share of violations does my monitor catch, and how many reviews are violations? | `baserate.scores.read_scores`, `baserate.estimate.recall_at_budget`, `baserate.card.build_card` |
| `examples/certify.py` | How many independent runs does a claim need, and how many more does an existing claim need? | `baserate.certify.zero_fp_runs`, `fpr_runs_for_power`, `joint_plan`, `more_runs_fpr` |
| `examples/build_cascade.py` | If a cheap monitor screens everything and escalates a share to a costly one, which split gives the most recall for the compute? | `baserate.cascade.sweep_cascade`, `CascadeSweep.best_under` |

## evaluate_your_monitor.py

Reads `examples/fixtures/monitor_scores.csv`, estimates recall and precision
when 20 of every 10,000 trajectories are reviewed at a prevalence of 1 in
1,000, and prints the full monitor report card: recall and precision at
budgets of 5, 20, and 100 per 10,000 and prevalences of 1e-2, 1e-3, and 1e-4,
the false-positive rate at the monitor's own decision threshold, and how many
more benign runs a false-positive claim would need. The command-line
equivalent is

```
baserate card examples/fixtures/monitor_scores.csv --name my-monitor --threshold 0.2
```

To evaluate your own monitor, write a score file in the same format
(docs/USAGE.md) and change `SCORES` at the top of the script, or run the
command above on your file.

## certify.py

Part 1 plans a study from design targets alone: the zero-event sample sizes
for a false-positive rate of at most 0.001 and a recall of at least 0.9, the
runs needed for 80% power when the true false-positive rate is half the
target, and both claims together at an overall 95%. Part 2 counts the benign
clusters with a false positive in the fixture at the monitor's threshold and
asks how many more independent benign runs the false-positive claim needs. The
command-line equivalent is `baserate certify --fpr 0.001 --recall 0.9`, with
`--have-runs N --have-events K` for an existing claim.

## build_cascade.py

Reads `examples/fixtures/cascade_scores.csv`, which has a stage-1 score (Armour's
`baseline` pack) and a stage-2 score (the `reviewer-calibrated` pack) for every
trace. It sweeps the share of traffic escalated to stage 2, prints the
cost-recall Pareto front, and picks the highest-recall point that costs at most
3 units per trajectory, with stage 1 at 1 unit and stage 2 at 10 units per
escalated trajectory. The costs are illustrative. The points are in-sample
estimates without intervals; on real data, choose the rule on one split of
source runs and test it on another with `baserate.cascade.split_by_run` and
`baserate.cascade.validate_holdout`. The command-line equivalent is

```
baserate cascade examples/fixtures/cascade_scores.csv \
    --prevalence 1e-3 --budget 20 --c1 1 --c2 10 --max-cost 3
```

## Fixtures

`examples/fixtures/README.md` states the source of the fixture data (Armour,
MIT License, commit 639eb62) and how `examples/fixtures/make_fixtures.py`
rebuilds them.
