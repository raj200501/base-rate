*Prevalence 1e-3, 20 reviews per 10,000, estimate [95% interval]. Set: primary. Traces: 3,418. Independent source runs: 2,800. Synthetic share: 0.181.*

| Monitor | Family | Recall | Precision |
|---|---|---|---|
| `rule:armour-baseline` | rule | 0.002 [0.000, 0.012] | 0.001 [0.000, 0.006] |
| `rule:armour-reviewer-calibrated` | rule | 0.002 [0.000, 0.013] | 0.001 [0.000, 0.007] |
| `rule:armour-strict-agent` | rule | 0.002 [0.000, 0.013] | 0.001 [0.000, 0.007] |
| `model:qwen3-0.6b-q8_0` | model | 0.001 [0.000, 0.012] | 0.000 [0.000, 0.006] |
| `model:qwen3-1.7b-q8_0` | model | 0.000 [0.000, 0.004] | 0.000 [0.000, 0.002] |
| `model:qwen3-4b-q4_k_m` | model | 0.000 [0.000, 0.004] | 0.000 [0.000, 0.002] |
| `model:qwen3-8b-q4_k_m` | model | 0.003 [0.000, 0.015] | 0.001 [0.000, 0.007] |
| rank mean of `rule:armour-reviewer-calibrated` and `model:qwen3-8b-q4_k_m` | ensemble | 0.069 [0.042, 0.102] | 0.034 [0.021, 0.051] |
| `model:qwen3-0.6b-q8_0` then `model:qwen3-8b-q4_k_m`, escalating 0.05 | cascade | 0.025 [0.009, 0.041] | 0.012 [0.004, 0.021] |
