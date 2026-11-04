# Example score files

Demonstration data, not study results. These files feed the three examples in
`examples/` and show the score-file format of docs/USAGE.md on real recorded
traces. No number computed from them is a finding about any monitor.

## Source

| Item | Value |
|---|---|
| Traces | Armour's labeled datasets, https://github.com/raj200501/armour |
| Armour commit | `639eb62ae17a8c3876137f375176417ff6671ea0` (639eb62) |
| License | MIT; the license text is in `ARMOUR_LICENSE` here (a copy of `tests/fixtures/armour/LICENSE`) |
| Local copy | `tests/fixtures/armour/datasets/*.jsonl`, verbatim, hash-checked against `tests/fixtures/armour/README.md` before use |
| Scores | Armour's rule packs as ported in `baserate.monitors.rules`; no model was called |
| Generator | `examples/fixtures/make_fixtures.py` (deterministic) |

Every trace is a recorded or scripted agent run on mock tools, and every
violation is an abstract mock action (for example a mock bulk export of a
`customers` table). Account ids, URLs, and tokens in the traces are
placeholders, not real systems or credentials. Most of the traces come from
Armour's deterministic generator scripts and are marked synthetic in the
`synthetic` column. The rule packs were written with Armour's own records in
view, and they read Armour's eval catalog for these eval ids (DECISIONS.md
D12), so these scores are not an unbiased test of the rules on new traffic.

## Files

- `monitor_scores.csv`: one monitor. `score` is the score of Armour's
  `reviewer-calibrated` rule pack. Used by `examples/evaluate_your_monitor.py`
  and `examples/certify.py`.
- `cascade_scores.csv`: two stages. `score` is Armour's `baseline` pack
  (stage 1) and `stage2_score` is the `reviewer-calibrated` pack (stage 2).
  Used by `examples/build_cascade.py`.
- `ARMOUR_LICENSE`: Armour's MIT license.

Both CSV files have the columns `trace_id`, `label`, `score`, `cluster`, and
`synthetic` (plus `stage2_score` in the cascade file), and begin with `#`
comment lines that state their source.

## Clusters

`cluster` is the trace's source file plus its agent family, for example
`datasets/local_agent_labeled_traces.jsonl:armour:armour-local-agent-labels-v0`.
Armour's records come from a few generator scripts, so a single record is not
an independent run (DECISIONS.md D12). Every interval computed from these
files therefore counts these few clusters, not the traces, which is why the
intervals are wide.

## Rebuilding

```
uv run python examples/fixtures/make_fixtures.py          # rewrite both files
uv run python examples/fixtures/make_fixtures.py --check  # exit 1 if they are out of date
```

`tests/test_examples.py` runs the check, so a change to the Armour copies, the
loader, or the rule packs that alters a score fails the tests until the files
are rebuilt and committed.
