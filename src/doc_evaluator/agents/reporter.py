"""Reporter: assemble findings into a scored evaluation report.

Division of labour: the **score is computed, the narrative is generated**.
Asking the model for the number would mean two runs over an identical
specification could disagree, which is disqualifying for an evaluation tool.
Asking it for the prose is exactly what it is good at. So arithmetic comes from
:func:`validation.score`, and the model writes the paragraphs around it.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..llm import ModelCallError, render_context, structured_call
from ..observability.instrument import timed
from ..schemas import ReportNarrative
from ..state import EvalState, Finding
from ..tools.registry import ToolRegistry
from ..validation import score as score_findings

REPORTER_SYSTEM = """\
You write the narrative layer of an API documentation evaluation.

The score, the grade and the severity counts are already computed and are given \
to you. Never recompute, contradict or hedge them — your job is to explain what \
they mean to someone deciding whether to integrate against this API.

Write for an engineer with limited time. Lead with the consequence, not the \
rule that was violated: "callers cannot tell a rate limit from an outage" says \
more than "429 is undocumented". Order the priorities by what unblocks an \
integrator soonest. Be direct about problems without being dramatic about them."""

REPORTER_PROMPT = """\
Write the narrative for this evaluation.

{context}

Produce an executive summary, the specification's genuine strengths, and an \
ordered list of remediation priorities."""

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}


def _top_findings(findings: list[Finding], limit: int = 8) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.rule_id))[:limit]


def _render_markdown(
    title: str,
    source: str,
    scoring: dict[str, Any],
    narrative: ReportNarrative,
    findings: list[Finding],
    artifacts: dict[str, Any],
    halt_reason: str | None,
) -> str:
    counts = scoring["severity_counts"]
    lines = [
        f"# API Documentation Evaluation — {title}",
        "",
        f"**Score {scoring['score']}/100 (grade {scoring['grade']})** · "
        f"{scoring['finding_count']} finding(s) across "
        f"{artifacts.get('operation_count', 0)} operation(s)",
        "",
        f"- Source: `{source}`",
        f"- Rules evaluated: {artifacts.get('rules_run', 0)} deterministic, "
        f"plus {artifacts.get('qualitative_findings', 0)} qualitative judgement(s)",
        f"- Severity: {counts['critical']} critical · {counts['major']} major · "
        f"{counts['minor']} minor · {counts['info']} info",
    ]

    if artifacts.get("redacted"):
        lines.append(
            f"- Redacted before analysis: {', '.join(artifacts['redacted'])} "
            "(credential material found in the source document)"
        )
    if halt_reason:
        lines.append(f"- ⚠️ **Partial evaluation** — {halt_reason}")
    if artifacts.get("qualitative_error"):
        lines.append(
            f"- ⚠️ Qualitative pass unavailable ({artifacts['qualitative_error']}); "
            "deterministic findings only."
        )

    lines += ["", "## Summary", "", narrative.executive_summary, ""]

    if narrative.strengths:
        lines += ["## What works", ""]
        lines += [f"- {s}" for s in narrative.strengths]
        lines.append("")

    if narrative.priorities:
        lines += ["## Remediation priorities", ""]
        lines += [f"{i}. {p}" for i, p in enumerate(narrative.priorities, 1)]
        lines.append("")

    if scoring["category_penalties"]:
        lines += ["## Where the points went", "", "| Category | Penalty |", "| --- | ---: |"]
        lines += [f"| {c} | −{p} |" for c, p in scoring["category_penalties"].items()]
        lines.append("")

    if findings:
        lines += [
            "## All findings",
            "",
            "| Severity | Rule | Location | Issue |",
            "| --- | --- | --- | --- |",
        ]
        for f in sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.rule_id)):
            message = f.message.replace("|", "\\|")
            lines.append(f"| {f.severity} | `{f.rule_id}` | `{f.json_path}` | {message} |")
        lines.append("")
    else:
        lines += ["## All findings", "", "None. The specification passed every rule.", ""]

    return "\n".join(lines)


def make_reporter(settings: Settings, registry: ToolRegistry):
    def reporter(state: EvalState) -> dict[str, Any]:
        with timed("node:reporter", "node"):
            artifacts = state.get("artifacts") or {}
            findings = list(state.get("findings") or [])
            scoring = state.get("score") or score_findings(findings)
            title = artifacts.get("title") or "Unknown API"
            top = _top_findings(findings)

            context = {
                "title": title,
                "score": scoring["score"],
                "grade": scoring["grade"],
                "severity_counts": scoring["severity_counts"],
                "dominant_category": scoring["dominant_category"],
                "operation_count": artifacts.get("operation_count", 0),
                "has_auth_scheme": artifacts.get("has_auth_scheme", False),
                "has_descriptions": any(
                    (op.get("summary") or op.get("description"))
                    for op in (artifacts.get("operations") or [])
                ),
                "has_examples": not any(
                    f.rule_id == "example.none_anywhere" for f in findings
                ),
                "top_findings": [
                    {"rule_id": f.rule_id, "severity": f.severity, "message": f.message}
                    for f in top
                ],
            }

            try:
                narrative = structured_call(
                    "reporter",
                    ReportNarrative,
                    REPORTER_PROMPT.format(context=render_context(context)),
                    settings,
                    system=REPORTER_SYSTEM,
                )
            except ModelCallError as exc:
                # A report without prose is still a usable report.
                narrative = ReportNarrative(
                    executive_summary=(
                        f"{title} scores {scoring['score']}/100 (grade {scoring['grade']}) "
                        f"with {scoring['finding_count']} finding(s). The narrative layer was "
                        f"unavailable ({exc}); the findings table below is complete."
                    ),
                    strengths=[],
                    priorities=[f.message for f in top],
                )

            report = _render_markdown(
                title=title,
                source=artifacts.get("source") or state.get("target", "?"),
                scoring=scoring,
                narrative=narrative,
                findings=findings,
                artifacts=artifacts,
                halt_reason=state.get("halt_reason"),
            )

            return {
                "completed": ["reporter"],
                "report": report,
                "score": scoring,
                "artifacts": {**artifacts, "narrative": narrative.model_dump()},
            }

    return reporter
