*Rule monitors at the fixed threshold 0.2, cluster-aware exact intervals (95%). Set: primary. Traces: 3,418. Independent source runs: 2,800. Synthetic share: 0.181.*

| Monitor | Recall | False-positive rate | Precision, prev. 1e-2 | Precision, prev. 1e-3 | Precision, prev. 1e-4 |
|---|---|---|---|---|---|
| `rule:armour-baseline` | 0.143 [0.122, 0.165] | 0.067 [0.058, 0.078] | 0.021 [0.015, 0.029] | 0.002 [0.001, 0.003] | 0.000 [0.000, 0.000] |
| `rule:armour-reviewer-calibrated` | 0.143 [0.122, 0.165] | 0.067 [0.058, 0.078] | 0.021 [0.015, 0.029] | 0.002 [0.001, 0.003] | 0.000 [0.000, 0.000] |
| `rule:armour-strict-agent` | 0.143 [0.122, 0.165] | 0.067 [0.058, 0.078] | 0.021 [0.015, 0.029] | 0.002 [0.001, 0.003] | 0.000 [0.000, 0.000] |
