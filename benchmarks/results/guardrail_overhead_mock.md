**Direct measurement — policy evaluation**

| Metric | Value |
| --- | ---: |
| Evaluations timed | 30,000 |
| Median per evaluation | **22.3 µs** |
| Mean per evaluation | 26.2 µs |
| p95 | 68.4 µs |
| p99 | 99.4 µs |
| Evaluations per evaluation-run | 3 |
| **Policy cost per run** | **0.067 ms** |

**End-to-end A/B — same graph, same fixtures, policy on vs. off**

| Metric | Guardrails off | Guardrails on | Delta |
| --- | ---: | ---: | ---: |
| Mean end-to-end | 15.06 ms | 13.93 ms | -1.14 ms (-7.55%) |
| p50 | 12.90 ms | 12.85 ms | -0.05 ms |
| p95 | 24.36 ms | 20.39 ms | -3.97 ms |
| Std dev | 6.22 ms | 3.21 ms | — |
| Tokens per run | 1,665 | 1,665 | +0 |
| Cost per run | $0.01644 | $0.01644 | $+0.00000 |

Noise band (2 standard errors on the difference of means): ±1.48 ms. The observed -1.14 ms delta is inside it, so the end-to-end cost of the policy layer is not resolvable at this sample size — which is the expected result when the thing being measured is ~0.07 ms.
