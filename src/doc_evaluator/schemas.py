"""Structured output contracts for every LLM call in the system.

No node in this graph consumes free-form model text as control flow. The
supervisor's route, the validator's qualitative judgements and the reporter's
narrative all come back as validated Pydantic objects. That is the same
structured-validation posture applied to the model itself: an LLM response is
untrusted input until a schema has accepted it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RouteTarget = Literal["fetcher", "validator", "reporter", "finish"]


class RouteDecision(BaseModel):
    """The supervisor's proposed next hop.

    Proposed, not final: ``agents/supervisor.py`` overrides this whenever a
    precondition or a budget ceiling disagrees. The model picks among legal
    moves; it does not get to define what is legal.
    """

    next: RouteTarget = Field(description="Which worker should run next, or 'finish'.")
    reason: str = Field(description="One sentence explaining the choice.", max_length=400)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("reason")
    @classmethod
    def _tidy(cls, v: str) -> str:
        return " ".join(v.split()) or "no reason given"


class QualitativeFinding(BaseModel):
    """A finding the deterministic rules cannot express.

    Deterministic rules answer "is `operationId` present?". These answer "is the
    description actually usable by an integrator?" — genuinely a judgement call,
    and the only place the model is allowed to author findings.
    """

    rule_id: str = Field(description="Stable id, prefixed 'llm.'.")
    severity: Literal["critical", "major", "minor", "info"]
    category: str = Field(default="clarity")
    json_path: str = Field(default="$", description="JSON path into the spec.")
    message: str = Field(max_length=500)
    evidence: str | None = Field(default=None, max_length=400)

    @field_validator("rule_id")
    @classmethod
    def _namespace(cls, v: str) -> str:
        v = v.strip() or "unspecified"
        return v if v.startswith("llm.") else f"llm.{v}"


class QualitativeReview(BaseModel):
    findings: list[QualitativeFinding] = Field(default_factory=list, max_length=25)
    summary: str = Field(default="", max_length=1000)


class ReportNarrative(BaseModel):
    """The prose layer of the report. Scores are computed, never generated."""

    executive_summary: str = Field(max_length=1500)
    strengths: list[str] = Field(default_factory=list, max_length=8)
    priorities: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Ordered remediation steps, highest impact first.",
    )
