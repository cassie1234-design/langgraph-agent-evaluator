"""Validator: deterministic rules first, model judgement second.

The ordering is the design. The rule set answers everything a machine can
answer exactly and costs nothing; the model is asked only about the residue —
whether the prose a human wrote is actually useful. Running the model over
questions the rules already settled would be slower, non-reproducible, and
would produce a different score on every run over the same document.

The model's output is not trusted either: it comes back through a schema, and
findings that duplicate a deterministic rule are dropped rather than counted
twice.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..llm import ModelCallError, render_context, structured_call
from ..observability.instrument import timed
from ..schemas import QualitativeReview
from ..state import EvalState, Finding
from ..tools.registry import ToolRegistry
from ..validation import score as score_findings

VALIDATOR_SYSTEM = """\
You review API documentation for the qualities a machine cannot check.

A deterministic rule set has already run and has reported every structural and \
completeness defect: missing operationIds, undocumented error responses, absent \
schemas, unapplied security schemes. Do not repeat any of that — those findings \
already exist and duplicating them inflates the report.

Judge only what requires reading comprehension:
  - Does the prose explain what the operation does, or does it restate the path?
  - Would an integrator know the preconditions, side effects and failure modes?
  - Is anything ambiguous, contradictory or stale?

Be specific and be sparing. A finding that says "could be clearer" is not \
actionable and is worse than no finding. Every finding must name what is wrong \
and imply what would fix it. Reporting nothing is correct when the prose is good."""

VALIDATOR_PROMPT = """\
Review the documentation prose for this API.

{context}

The deterministic rules already reported {deterministic_count} finding(s); those \
are listed as `already_reported_rule_ids` above so you can avoid restating them.

Return only the qualitative issues that reading the prose reveals."""


def make_validator(settings: Settings, registry: ToolRegistry):
    def validator(state: EvalState) -> dict[str, Any]:
        with timed("node:validator", "node"):
            artifacts = state.get("artifacts") or {}
            spec = artifacts.get("spec")

            if not spec:
                return {
                    "completed": ["validator"],
                    "artifacts": {**artifacts, "last_error": "no specification to validate"},
                }

            # -- pass 1: deterministic -------------------------------------
            result = registry.call("validate_spec", spec=spec)
            events = [result.event] if result.event else []

            if not result.ok:
                return {
                    "completed": ["validator"],
                    "artifacts": {**artifacts, "last_error": result.error},
                    "guardrail_log": events,
                }

            deterministic = [Finding.model_validate(f) for f in result.value["findings"]]
            seen_rules = {f.rule_id for f in deterministic}
            # Dedupe by *location*, not just by rule id. The model is told not to
            # repeat the deterministic findings, but it repeats them under its own
            # rule id — "llm.no_prose" and "op.no_documentation" are the same
            # observation about the same operation. Matching on (path, category)
            # catches that; matching on rule_id alone never can.
            seen_locations = {(f.json_path, f.category) for f in deterministic}

            # -- pass 2: qualitative ---------------------------------------
            security = (spec.get("components") or {}).get("securitySchemes") or {}
            auth_applied = bool(spec.get("security")) or any(
                op.get("security") for op in _operations_of(spec)
            )
            context = {
                "title": artifacts.get("title"),
                "operations": artifacts.get("operations") or [],
                "has_auth_scheme": bool(security),
                "auth_applied": auth_applied,
                "already_reported_rule_ids": sorted(seen_rules),
                "already_reported_locations": sorted(
                    f"{path} ({category})" for path, category in seen_locations
                ),
            }

            qualitative: list[Finding] = []
            llm_error: str | None = None
            try:
                review = structured_call(
                    "validator",
                    QualitativeReview,
                    VALIDATOR_PROMPT.format(
                        context=render_context(context),
                        deterministic_count=len(deterministic),
                    ),
                    settings,
                    system=VALIDATOR_SYSTEM,
                )
                for item in review.findings:
                    if item.rule_id in seen_rules:
                        continue  # the rules already said this
                    if (item.json_path, item.category) in seen_locations:
                        continue  # same observation, different rule id
                    seen_locations.add((item.json_path, item.category))
                    qualitative.append(
                        Finding(
                            rule_id=item.rule_id,
                            severity=item.severity,
                            category=item.category,
                            json_path=item.json_path,
                            message=item.message,
                            evidence=item.evidence,
                            source="llm",
                        )
                    )
            except ModelCallError as exc:
                # Degrade to the deterministic findings. A partial evaluation
                # with an honest note beats no evaluation.
                llm_error = str(exc)

            findings = deterministic + qualitative
            return {
                "completed": ["validator"],
                "findings": findings,
                "score": score_findings(findings),
                "artifacts": {
                    **artifacts,
                    "rules_run": result.value["rules_run"],
                    "deterministic_findings": len(deterministic),
                    "qualitative_findings": len(qualitative),
                    "qualitative_error": llm_error,
                    "last_error": None,
                },
                "guardrail_log": events,
            }

    return validator


def _operations_of(spec: dict[str, Any]):
    from ..validation import iter_operations

    return [op for _p, _m, op in iter_operations(spec)]
