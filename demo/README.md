# Offline demo

`make demo` runs `python -m baserate.demo` with no network and no model calls
(`BASERATE_NO_MODEL_CALLS=1`, `uv --offline`) and compares its output with
`golden_output.txt`. It loads Armour's recorded labeled traces (MIT; copies and
SHA-256 hashes under tests/fixtures/armour), scores them with the three Armour
rule monitors, and prints fixed-threshold rates with independent and
group-level intervals, recall and precision at review budgets, what the data
can certify, and a cascade sweep with illustrative costs.

The data are synthetic and recorded mock-tool traces, so the output is a
demonstration of the method, not a study result. Regenerate the golden file
only when the demo or the library changes on purpose:

```bash
BASERATE_NO_MODEL_CALLS=1 uv run --frozen python -m baserate.demo --out demo/golden_output.txt
```
