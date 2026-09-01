"""Graph state and the domain types that flow through it.

Design note: the state is deliberately *flat and explicit*. A common failure
mode in supervisor architectures is stuffing everything into the message list
and asking the router to re-read the whole transcript on every hop — which is
both expensive and non-deterministic. Here the message list carries narration
only; routing decisions read typed fields (``artifacts``, ``findings``,
``completed``) that a worker either populated or did not.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

Severity = Literal["critical", "major", "minor", "info"]
WorkerName = Literal["fetcher", "validator", "reporter"]

SEVERITY_WEIGHT: dict[Severity, int] = {
    "critical": 40,
    "major": 15,
    "minor": 4,
    "info": 0,
}


class Finding(BaseModel):
    """One validation result. Deterministic and LLM-judged rules emit the same shape."""

    rule_id: str
    severity: Severity
    category: str = "general"
    json_path: str = "$"
    message: str
    evidence: str | None = None
    source: Literal["deterministic", "llm"] = "deterministic"

    def key(self) -> tuple[str, str]:
        return (self.rule_id, self.json_path)


class GuardrailEvent(BaseModel):
    """Audit record for a single intercepted tool call."""

    tool: str
    tier: str
    action: str
    reason: str
    matched_rules: list[str] = Field(default_factory=list)
    approved: bool | None = None
    eval_micros: float = 0.0
    args_digest: str = ""


class RouteRecord(BaseModel):
    """One supervisor hop, kept so the UI can render *why* each worker ran."""

    iteration: int
    decision: str
    reason: str
    source: Literal["model", "precondition", "budget", "completion"] = "model"
    confidence: float | None = None


def _merge_findings(left: list[Finding], right: list[Finding]) -> list[Finding]:
    """De-duplicate on (rule_id, json_path) so a re-run cannot inflate the score."""
    merged: dict[tuple[str, str], Finding] = {f.key(): f for f in left}
    for finding in right:
        merged.setdefault(finding.key(), finding)
    return list(merged.values())


class EvalState(TypedDict, total=False):
    """The single state object threaded through every node."""

    target: str
    messages: Annotated[list, add_messages]

    artifacts: dict[str, Any]
    findings: Annotated[list[Finding], _merge_findings]
    report: str | None
    score: dict[str, Any] | None

    completed: Annotated[list[str], operator.add]
    route_log: Annotated[list[RouteRecord], operator.add]
    guardrail_log: Annotated[list[GuardrailEvent], operator.add]

    iteration: int
    next_worker: str | None
    halt_reason: str | None
