"""Deterministic stand-in for the live model.

This is not a stub that returns fixed strings. It reads the same
``<eval_context>`` payload the live model reads and derives a defensible answer
from it, so a mock run exercises the real routing conditions, the real
guardrail triggers and the real scoring path. Recorded cassettes take priority
when one matches, which is what lets a live run be captured and replayed
verbatim.

It also reports token usage (estimated from text length at the standard ~4
chars/token heuristic) so the ledger and the benchmark produce comparable
numbers in both modes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..schemas import (
    QualitativeFinding,
    QualitativeReview,
    ReportNarrative,
    RouteDecision,
)
from .prompt_context import extract


def estimate_tokens(text: str) -> int:
    """~4 characters per token. Good enough for relative comparisons."""
    return max(1, len(text) // 4)


def cassette_key(node: str, schema_name: str, prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    return f"{node}:{schema_name}:{digest}"


class CassetteStore:
    """Recorded live responses, keyed by (node, schema, prompt digest)."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            merged: dict[str, Any] = {}
            if self.directory.is_dir():
                for path in sorted(self.directory.glob("*.json")):
                    try:
                        data = json.loads(path.read_text())
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        merged.update(data)
            self._cache = merged
        return self._cache

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._load().get(key)
        return entry if isinstance(entry, dict) else None


# --------------------------------------------------------------------------
# Deterministic synthesis, per node.
# --------------------------------------------------------------------------


def _synth_route(ctx: dict[str, Any]) -> dict[str, Any]:
    """Mirror the ordering a competent router would choose from the same facts."""
    completed = set(ctx.get("completed") or [])
    has_spec = bool(ctx.get("has_artifact"))
    finding_count = int(ctx.get("finding_count") or 0)
    has_report = bool(ctx.get("has_report"))

    if not has_spec:
        return {
            "next": "fetcher",
            "reason": "No specification has been retrieved yet, so nothing can be validated.",
            "confidence": 0.95,
        }
    if "validator" not in completed:
        return {
            "next": "validator",
            "reason": "The specification is in hand but has not been validated.",
            "confidence": 0.9,
        }
    if not has_report:
        return {
            "next": "reporter",
            "reason": f"Validation produced {finding_count} findings; they need a report.",
            "confidence": 0.88,
        }
    return {
        "next": "finish",
        "reason": "Fetch, validation and reporting are all complete.",
        "confidence": 0.92,
    }


_VAGUE = {"", "todo", "tbd", "n/a", "description", "the endpoint", "endpoint", "api"}


def _synth_review(ctx: dict[str, Any]) -> dict[str, Any]:
    """Judge description quality from the operation digest in the context block."""
    findings: list[dict[str, Any]] = []
    operations = ctx.get("operations") or []
    # Mirror the instruction the live model is given: do not restate a finding
    # the deterministic rules already made about this location.
    covered = set(ctx.get("already_reported_locations") or [])

    for op in operations[:25]:
        path = op.get("path", "?")
        method = str(op.get("method", "get")).upper()
        summary = (op.get("summary") or "").strip()
        description = (op.get("description") or "").strip()
        blob = f"{summary} {description}".strip()

        location = f"$.paths['{path}'].{method.lower()}"
        if f"{location} (clarity)" in covered:
            continue

        if not blob:
            findings.append(
                {
                    "rule_id": "llm.no_prose",
                    "severity": "major",
                    "category": "clarity",
                    "json_path": location,
                    "message": (
                        f"{method} {path} carries neither a summary nor a description, so an "
                        "integrator cannot tell what it does without reading the schema."
                    ),
                    "evidence": None,
                }
            )
        elif blob.lower() in _VAGUE or len(blob) < 25:
            findings.append(
                {
                    "rule_id": "llm.thin_prose",
                    "severity": "minor",
                    "category": "clarity",
                    "json_path": location,
                    "message": (
                        f"{method} {path} is documented as {blob!r}, which restates the path "
                        "rather than explaining behaviour, preconditions or side effects."
                    ),
                    "evidence": blob[:200],
                }
            )

    if ctx.get("has_auth_scheme") and not ctx.get("auth_applied"):
        findings.append(
            {
                "rule_id": "llm.auth_unexplained",
                "severity": "major",
                "category": "usability",
                "json_path": "$.components.securitySchemes",
                "message": (
                    "A security scheme is declared but no operation references it, so the "
                    "documentation never tells a reader how to authenticate."
                ),
                "evidence": None,
            }
        )

    total = len(operations) or 1
    summary = (
        f"Reviewed {total} operation(s); {len(findings)} qualitative issue(s) worth raising."
        if findings
        else f"Reviewed {total} operation(s); prose quality is adequate throughout."
    )
    return {"findings": findings[:25], "summary": summary}


def _synth_narrative(ctx: dict[str, Any]) -> dict[str, Any]:
    counts = ctx.get("severity_counts") or {}
    score = ctx.get("score", 0)
    title = ctx.get("title") or "the API"
    top = ctx.get("top_findings") or []

    critical = counts.get("critical", 0)
    major = counts.get("major", 0)

    if critical:
        verdict = "not ready for external consumers"
    elif major:
        verdict = "usable but incomplete for external consumers"
    else:
        verdict = "in good shape for external consumers"

    strengths: list[str] = []
    if ctx.get("has_descriptions"):
        strengths.append("Operations carry human-readable summaries.")
    if ctx.get("has_auth_scheme"):
        strengths.append("Authentication is declared in the specification.")
    if ctx.get("has_examples"):
        strengths.append("Request and response examples are present.")
    if not critical:
        strengths.append("No critical structural defects were found.")

    return {
        "executive_summary": (
            f"{title} scores {score}/100 and is {verdict}. Validation surfaced "
            f"{counts.get('critical', 0)} critical, {counts.get('major', 0)} major and "
            f"{counts.get('minor', 0)} minor issues. The dominant theme is "
            f"{ctx.get('dominant_category', 'documentation completeness')}."
        ),
        "strengths": strengths[:8] or ["The specification parses cleanly."],
        "priorities": [f["message"] for f in top][:8]
        or ["No remediation required; keep the specification in sync with the implementation."],
    }


SYNTHESIZERS = {
    "supervisor": _synth_route,
    "validator": _synth_review,
    "reporter": _synth_narrative,
}

SCHEMA_BY_NAME = {
    "RouteDecision": RouteDecision,
    "QualitativeReview": QualitativeReview,
    "QualitativeFinding": QualitativeFinding,
    "ReportNarrative": ReportNarrative,
}


class ReplayModel:
    """Mock counterpart to a ``ChatAnthropic`` structured call."""

    def __init__(self, cassette_dir: Path, model: str = "claude-opus-5") -> None:
        self.store = CassetteStore(cassette_dir)
        self.model = model

    def structured(self, node: str, schema: type, prompt: str) -> tuple[Any, int, int]:
        """Return ``(parsed_object, input_tokens, output_tokens)``."""
        key = cassette_key(node, schema.__name__, prompt)
        recorded = self.store.get(key)
        if recorded is not None:
            payload = recorded.get("output", recorded)
        else:
            synth = SYNTHESIZERS.get(node)
            if synth is None:
                raise LookupError(
                    f"no cassette for {key!r} and no synthesizer registered for node {node!r}"
                )
            payload = synth(extract(prompt))

        parsed = schema.model_validate(payload)
        out_text = json.dumps(payload, default=str)
        return parsed, estimate_tokens(prompt), estimate_tokens(out_text)
