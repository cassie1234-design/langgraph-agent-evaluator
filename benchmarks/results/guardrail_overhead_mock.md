**Direct measurement — policy evaluation**

| Metric | Value |
| --- | ---: |
| Evaluations timed | 4,000 |
| Median per evaluation | **23.1 µs** |
| Mean per evaluation | 27.9 µs |
| p95 | 74.8 µs |
| p99 | 115.5 µs |
| Evaluations per evaluation-run | 3 |
| **Policy cost per run** | **0.069 ms** |

**End-to-end A/B — same graph, same fixtures, policy on vs. off**

| Metric | Guardrails off | Guardrails on | Delta |
| --- | ---: | ---: | ---: |
| Mean end-to-end | 13.20 ms | 13.63 ms | +0.43 ms (+3.27%) |
| p50 | 11.80 ms | 12.41 ms | +0.61 ms |
| p95 | 16.83 ms | 18.00 ms | +1.18 ms |
| Std dev | 3.25 ms | 2.67 ms | — |
| Tokens per run | 1,585 | 1,585 | +0 |
| Cost per run | $0.01705 | $0.01705 | $+0.00000 |

Noise band (2 standard errors on the difference of means): ±1.98 ms. The observed +0.43 ms delta is inside it, so the end-to-end cost of the policy layer is not resolvable at this sample size — which is the expected result when the thing being measured is ~0.07 ms.
