"""The guarded tool registry.

Every tool call in the system goes through :meth:`ToolRegistry.call`. There is
no second path — workers hold a registry, not a function reference — so the
question "can this tool run without policy evaluation?" has a structural answer
rather than a code-review answer.

The registry decides; it does not ask. Human approval is delegated to an
injected ``approver`` callable. In the graph that callable is LangGraph's
``interrupt()``, which suspends the run and persists it to the checkpointer. In
tests it is a lambda. That seam is why the HIGH_RISK path is testable at all —
otherwise asserting on it would mean driving a UI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..guardrails.engine import Action, Decision, GuardrailEngine
from ..observability.instrument import timed
from ..state import GuardrailEvent
from . import doc_tools, risky_tools

Approver = Callable[[Decision, dict[str, Any]], bool]

TOOLS: dict[str, Callable[..., Any]] = {
    "fetch_document": doc_tools.fetch_document,
    "parse_openapi": doc_tools.parse_openapi,
    "validate_spec": doc_tools.validate_spec,
    "purge_cache": risky_tools.purge_cache,
    "send_external_report": risky_tools.send_external_report,
}


@dataclass
class ToolResult:
    """Outcome of a guarded call: what happened, and why it was allowed to."""

    tool: str
    ok: bool
    value: Any = None
    error: str | None = None
    decision: Decision | None = None
    event: GuardrailEvent | None = None
    refused: bool = False

    @property
    def blocked_by_policy(self) -> bool:
        return self.refused


def deny_all_approver(decision: Decision, args: dict[str, Any]) -> bool:
    """Default approver: deny.

    An unattended run — CI, a scheduled evaluation, a test — has nobody to ask.
    Defaulting to *approve* would mean the HIGH_RISK tier silently degrades to
    SAFE exactly when no human is watching, which inverts the point of the tier.
    """
    return False


def interrupt_approver(decision: Decision, args: dict[str, Any]) -> bool:
    """Suspend the graph and surface the decision to a human.

    ``interrupt()`` raises out of the node; LangGraph checkpoints the run and
    returns control to the caller, which renders the payload below. The run
    resumes with ``Command(resume={"approved": bool})``.
    """
    from langgraph.types import interrupt

    answer = interrupt(
        {
            "kind": "guardrail_approval",
            "tool": decision.tool,
            "risk_tier": decision.tier.value,
            "reason": decision.reason,
            "matched_rules": list(decision.matched_rules),
            "arguments": {k: _preview(v) for k, v in args.items()},
            "question": f"Allow the agent to call {decision.tool!r}?",
        }
    )
    if isinstance(answer, dict):
        return bool(answer.get("approved", False))
    return bool(answer)


def _preview(value: Any, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + f"… (+{len(text) - limit} chars)"


@dataclass
class ToolRegistry:
    settings: Settings
    engine: GuardrailEngine | None = None
    approver: Approver = deny_all_approver
    tools: dict[str, Callable[..., Any]] = field(default_factory=lambda: dict(TOOLS))

    def __post_init__(self) -> None:
        if self.engine is None:
            self.engine = GuardrailEngine(
                allowed_hosts=self.settings.allowed_hosts,
                enabled=self.settings.guardrails_enabled,
            )

    def call(self, tool: str, **args: Any) -> ToolResult:
        assert self.engine is not None

        # 1. Evaluate. Timed as its own span so the benchmark can price it.
        with timed(f"guardrail:{tool}", "guardrail") as span:
            decision = self.engine.evaluate(tool, args)
        span.detail.update(
            {"tier": decision.tier.value, "action": decision.action.value,
             "matched": list(decision.matched_rules)}
        )

        event = GuardrailEvent(
            tool=tool,
            tier=decision.tier.value,
            action=decision.action.value,
            reason=decision.reason,
            matched_rules=list(decision.matched_rules),
            eval_micros=decision.eval_micros,
            args_digest=decision.args_digest,
        )

        # 2. Hard block. The agent gets a structured refusal, not an exception.
        if decision.action is Action.BLOCK:
            return ToolResult(
                tool=tool,
                ok=False,
                value=decision.refusal_payload(),
                error=decision.reason,
                decision=decision,
                event=event,
                refused=True,
            )

        # 3. Human approval. May raise out of the node via interrupt().
        if decision.action is Action.CONFIRM:
            approved = bool(self.approver(decision, args))
            event.approved = approved
            if not approved:
                payload = decision.refusal_payload()
                payload["status"] = "denied_by_operator"
                payload["guidance"] = (
                    "A human declined this call. Do not retry it; continue without it "
                    "and record the omission in the report."
                )
                return ToolResult(
                    tool=tool,
                    ok=False,
                    value=payload,
                    error="denied by operator",
                    decision=decision,
                    event=event,
                    refused=True,
                )

        # 4. Execute.
        func = self.tools.get(tool)
        if func is None:
            return ToolResult(
                tool=tool,
                ok=False,
                error=f"no implementation registered for tool {tool!r}",
                decision=decision,
                event=event,
            )

        call_args = dict(args)
        if "settings" not in call_args:
            call_args["settings"] = self.settings

        try:
            with timed(f"tool:{tool}", "tool") as tool_span:
                value = func(**call_args)
            tool_span.detail["tier"] = decision.tier.value
        except Exception as exc:
            return ToolResult(
                tool=tool,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                decision=decision,
                event=event,
            )

        return ToolResult(tool=tool, ok=True, value=value, decision=decision, event=event)
