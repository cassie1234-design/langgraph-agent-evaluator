#!/usr/bin/env python3
"""Measure what the guardrail layer costs.

The headline question this project needs a real number for: *does policy
evaluation on every tool call make the system meaningfully slower?*

Method, and why it is shaped this way:

* **Mock mode by default.** A live run's latency is dominated by network and
  model variance measured in hundreds of milliseconds. Guardrail evaluation is
  measured in microseconds. Trying to see the second through the first needs a
  sample size nobody will wait for. Replay mode removes that noise, so what is
  left is the framework overhead the guardrails actually add — which is the
  thing being measured.
* **Same graph, same fixtures, both arms.** The control arm flips
  ``guardrails_enabled`` off, which makes the engine return ALLOW without
  evaluating. Everything else is identical.
* **Warmup discarded.** The first iteration pays for policy-file parsing and
  import; reporting it as steady-state cost would overstate the overhead.

``--live`` runs the same comparison against the real API with a smaller N, for
when the question is end-to-end user-visible latency rather than isolated
overhead.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from doc_evaluator.config import Settings  # noqa: E402
from doc_evaluator.graph import Evaluation  # noqa: E402
from doc_evaluator.guardrails.engine import GuardrailEngine  # noqa: E402
from doc_evaluator.samples import SYNTHETIC_CREDENTIAL  # noqa: E402

FIXTURES = ["petstore.json", "legacy_billing.yaml", "broken_inventory.json"]
RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"

# A representative spread: cheap allowlist hits, escalating rules, and the
# blocked cases. Benchmarking only the SAFE path would flatter the result.
POLICY_CALLS = [
    ("fetch_document", {"url": "https://petstore3.swagger.io/api/v3/openapi.json"}),
    ("fetch_document", {"url": "https://cdn.unknown-vendor.example/spec.json"}),
    ("fetch_document", {"url": "http://169.254.169.254/latest/meta-data/"}),
    ("fetch_document", {"url": "fixtures/specs/petstore.json"}),
    ("parse_openapi", {"text": "{}"}),
    ("validate_spec", {"spec": {}}),
    ("purge_cache", {"pattern": "specs/petstore.json"}),
    ("purge_cache", {"pattern": "*"}),
    ("send_external_report", {"webhook_url": "https://petstore3.swagger.io/h", "body": "ok"}),
    (
        "send_external_report",
        {
            "webhook_url": "https://petstore3.swagger.io/h",
            "body": f"key {SYNTHETIC_CREDENTIAL}",
        },
    ),
]


def microbenchmark_policy(settings: Settings, iterations: int) -> dict:
    """Time ``engine.evaluate`` directly.

    The end-to-end A/B below cannot resolve this cost — it is microseconds
    hiding inside milliseconds of run-to-run variance. Measuring the function
    itself is the only way to get a number with more signal than noise in it,
    so it is reported as the headline and the A/B is reported as the sanity
    check it actually is.
    """
    engine = GuardrailEngine(allowed_hosts=settings.allowed_hosts)
    for tool, args in POLICY_CALLS:  # warm the caches
        engine.evaluate(tool, args)

    per_call_us: list[float] = []
    for _ in range(iterations):
        for tool, args in POLICY_CALLS:
            start = time.perf_counter()
            engine.evaluate(tool, args)
            per_call_us.append((time.perf_counter() - start) * 1e6)

    by_tool: dict[str, float] = {}
    for tool, args in POLICY_CALLS:
        samples = []
        for _ in range(max(iterations // 4, 50)):
            start = time.perf_counter()
            engine.evaluate(tool, args)
            samples.append((time.perf_counter() - start) * 1e6)
        key = f"{tool}({next(iter(args))}={str(next(iter(args.values())))[:34]})"
        by_tool[key] = statistics.median(samples)

    return {
        "evaluations": len(per_call_us),
        "mean_us": statistics.fmean(per_call_us),
        "median_us": statistics.median(per_call_us),
        "p95_us": percentile(per_call_us, 0.95),
        "p99_us": percentile(per_call_us, 0.99),
        "by_call_median_us": dict(sorted(by_tool.items(), key=lambda kv: -kv[1])),
    }


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(int(round(q * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[index]


def run_once(target: str, settings: Settings) -> dict[str, float]:
    run = Evaluation(settings=settings, approver=lambda decision, args: False)
    start = time.perf_counter()
    result = run.start(target)
    wall_ms = (time.perf_counter() - start) * 1000
    summary = result.ledger.summary()
    return {
        "wall_ms": wall_ms,
        "guardrail_ms": summary["guardrail_total_ms"],
        "guardrail_evaluations": summary["guardrail_evaluations"],
        "tokens": summary["total_tokens"],
        "usd": summary["total_usd"],
        "score": (result.score or {}).get("score", 0),
        "findings": len(result.findings),
    }


def paired_arms(base: Settings, repeats: int, warmup: int) -> tuple[dict, dict]:
    """Run both arms interleaved rather than one after the other.

    Running all of the control arm and then all of the treatment arm lets any
    drift over the benchmark's lifetime — page cache, allocator state, CPU
    frequency, other load on the box — land entirely on one arm and read as
    signal. An earlier version of this script did exactly that and reported a
    +2.4 ms guardrail cost, 34x larger than the direct measurement of the same
    code. Alternating on/off within each pair cancels drift to first order.
    """
    on_settings = base.replace(guardrails_enabled=True)
    off_settings = base.replace(guardrails_enabled=False)

    on_samples: list[dict[str, float]] = []
    off_samples: list[dict[str, float]] = []

    for fixture in FIXTURES:
        target = str(REPO_ROOT / "fixtures" / "specs" / fixture)
        for _ in range(warmup):
            run_once(target, off_settings)
            run_once(target, on_settings)

        for index in range(repeats):
            # Alternate which arm goes first within the pair, so neither one
            # systematically pays the cost of being the cold half.
            order = (off_settings, on_settings) if index % 2 == 0 else (on_settings, off_settings)
            for settings in order:
                sample = run_once(target, settings)
                sample["fixture"] = fixture
                (on_samples if settings.guardrails_enabled else off_samples).append(sample)

    return _summarise("guardrails_on", on_samples), _summarise("guardrails_off", off_samples)


def _summarise(label: str, samples: list[dict[str, float]]) -> dict:
    walls = [s["wall_ms"] for s in samples]
    return {
        "label": label,
        "runs": len(samples),
        "mean_ms": statistics.fmean(walls),
        "median_ms": statistics.median(walls),
        "p50_ms": percentile(walls, 0.50),
        "p95_ms": percentile(walls, 0.95),
        "stdev_ms": statistics.pstdev(walls),
        "guardrail_ms_mean": statistics.fmean([s["guardrail_ms"] for s in samples]),
        "guardrail_evaluations": statistics.fmean([s["guardrail_evaluations"] for s in samples]),
        "tokens_mean": statistics.fmean([s["tokens"] for s in samples]),
        "usd_mean": statistics.fmean([s["usd"] for s in samples]),
        "samples": samples,
    }


def markdown_table(on: dict, off: dict, micro: dict) -> str:
    delta_ms = on["mean_ms"] - off["mean_ms"]
    pct = (delta_ms / off["mean_ms"] * 100) if off["mean_ms"] else 0.0
    noise = noise_band(on, off)

    lines = [
        "**Direct measurement — policy evaluation**",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Evaluations timed | {micro['evaluations']:,} |",
        f"| Median per evaluation | **{micro['median_us']:.1f} µs** |",
        f"| Mean per evaluation | {micro['mean_us']:.1f} µs |",
        f"| p95 | {micro['p95_us']:.1f} µs |",
        f"| p99 | {micro['p99_us']:.1f} µs |",
        f"| Evaluations per evaluation-run | {on['guardrail_evaluations']:.0f} |",
        f"| **Policy cost per run** | **{policy_cost_ms(micro, on):.3f} ms** |",
        "",
        "**End-to-end A/B — same graph, same fixtures, policy on vs. off**",
        "",
        "| Metric | Guardrails off | Guardrails on | Delta |",
        "| --- | ---: | ---: | ---: |",
        f"| Mean end-to-end | {off['mean_ms']:.2f} ms | {on['mean_ms']:.2f} ms | "
        f"{delta_ms:+.2f} ms ({pct:+.2f}%) |",
        f"| p50 | {off['p50_ms']:.2f} ms | {on['p50_ms']:.2f} ms | "
        f"{on['p50_ms'] - off['p50_ms']:+.2f} ms |",
        f"| p95 | {off['p95_ms']:.2f} ms | {on['p95_ms']:.2f} ms | "
        f"{on['p95_ms'] - off['p95_ms']:+.2f} ms |",
        f"| Std dev | {off['stdev_ms']:.2f} ms | {on['stdev_ms']:.2f} ms | — |",
        f"| Tokens per run | {off['tokens_mean']:,.0f} | {on['tokens_mean']:,.0f} | "
        f"{on['tokens_mean'] - off['tokens_mean']:+,.0f} |",
        f"| Cost per run | ${off['usd_mean']:.5f} | ${on['usd_mean']:.5f} | "
        f"${on['usd_mean'] - off['usd_mean']:+.5f} |",
        "",
        f"Noise band (2 standard errors on the difference of means): ±{noise:.2f} ms. "
        + (
            f"The observed {delta_ms:+.2f} ms delta is inside it, so the end-to-end "
            "cost of the policy layer is not resolvable at this sample size — which "
            "is the expected result when the thing being measured is ~"
            f"{policy_cost_ms(micro, on):.2f} ms."
            if abs(delta_ms) <= noise
            else f"The observed {delta_ms:+.2f} ms delta is outside it and is a real effect."
        ),
    ]
    return "\n".join(lines)


def policy_cost_ms(micro: dict, on: dict) -> float:
    """Predicted per-run policy cost: median evaluation time x evaluations per run."""
    return micro["median_us"] * on["guardrail_evaluations"] / 1000


def noise_band(on: dict, off: dict) -> float:
    """Two standard errors on the difference of the two means."""
    var_on = on["stdev_ms"] ** 2 / max(on["runs"], 1)
    var_off = off["stdev_ms"] ** 2 / max(off["runs"], 1)
    return 2 * (var_on + var_off) ** 0.5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repeats", type=int, default=15, help="Timed runs per fixture.")
    parser.add_argument("--warmup", type=int, default=2, help="Discarded runs per fixture.")
    parser.add_argument("--live", action="store_true", help="Use real Claude calls.")
    parser.add_argument(
        "--policy-iterations",
        type=int,
        default=2000,
        help="Rounds of the direct policy microbenchmark (10 calls per round).",
    )
    parser.add_argument("--out", type=Path, default=None, help="Where to write the JSON result.")
    args = parser.parse_args(argv)

    mode = "live" if args.live else "mock"
    if args.live:
        args.repeats = min(args.repeats, 3)
        args.warmup = min(args.warmup, 1)

    base = Settings.from_env(mode=mode)
    if args.live and not base.api_key:
        print("error: --live requires ANTHROPIC_API_KEY", file=sys.stderr)
        return 2

    total = len(FIXTURES) * (args.repeats + args.warmup) * 2
    print(
        f"Benchmarking guardrail overhead — mode={mode}, {len(FIXTURES)} fixtures, "
        f"{args.repeats} timed runs each ({args.warmup} warmup), {total} runs total.\n"
    )

    print(f"  [1/3] direct policy microbenchmark ({args.policy_iterations:,} rounds) …")
    micro = microbenchmark_policy(base, args.policy_iterations)
    print("  [2/3] end-to-end A/B, arms interleaved …")
    on, off = paired_arms(base, args.repeats, args.warmup)
    print("  [3/3] summarising …")

    delta_ms = on["mean_ms"] - off["mean_ms"]
    pct = (delta_ms / off["mean_ms"] * 100) if off["mean_ms"] else 0.0

    payload = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "mode": mode,
        "model": base.model,
        "fixtures": FIXTURES,
        "repeats_per_fixture": args.repeats,
        "warmup_per_fixture": args.warmup,
        "policy_microbenchmark": micro,
        "guardrails_off": {k: v for k, v in off.items() if k != "samples"},
        "guardrails_on": {k: v for k, v in on.items() if k != "samples"},
        "delta": {
            "mean_ms": delta_ms,
            "mean_pct": pct,
            "noise_band_ms": noise_band(on, off),
            "within_noise": abs(delta_ms) <= noise_band(on, off),
            "policy_cost_per_run_ms": policy_cost_ms(micro, on),
            "p95_ms": on["p95_ms"] - off["p95_ms"],
            "tokens": on["tokens_mean"] - off["tokens_mean"],
            "usd": on["usd_mean"] - off["usd_mean"],
        },
        "samples": {"guardrails_off": off["samples"], "guardrails_on": on["samples"]},
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = args.out or RESULTS_DIR / f"guardrail_overhead_{mode}.json"
    out.write_text(json.dumps(payload, indent=2))

    table = markdown_table(on, off, micro)
    (RESULTS_DIR / f"guardrail_overhead_{mode}.md").write_text(table + "\n")

    print("\n" + table)
    print("\nSlowest policy decisions (median):")
    for call, us in list(micro["by_call_median_us"].items())[:4]:
        print(f"  {us:7.1f} µs  {call}")
    print(
        f"\nHeadline: {micro['median_us']:.1f} µs per policy evaluation, "
        f"{on['guardrail_evaluations']:.0f} evaluations per run "
        f"= {policy_cost_ms(micro, on):.3f} ms of policy cost."
    )
    print(f"\njson → {out}\nmarkdown → {RESULTS_DIR / f'guardrail_overhead_{mode}.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
