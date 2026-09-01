"""Deterministic validation rules for OpenAPI documents.

Every rule is a pure function ``spec -> list[Finding]``. That shape is what
makes the whole layer testable without a model, a network, or a graph: the
test suite feeds fixtures in and asserts on rule ids.

Split of responsibility with the LLM pass:

* a rule lives here if a machine can answer it exactly — is the field present,
  is the reference resolvable, is the status code documented;
* it lives in the LLM pass if answering it requires judgement — is the prose
  actually informative.

Anything a rule can decide is decided by a rule. The model is expensive,
non-deterministic and unnecessary for "does this key exist".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

from ..state import SEVERITY_WEIGHT, Finding, Severity
from .meta_schema import HTTP_METHODS, OPENAPI_STRUCTURE

Rule = Callable[[dict[str, Any]], list[Finding]]
_RULES: list[Rule] = []


def rule(func: Rule) -> Rule:
    _RULES.append(func)
    return func


def _finding(
    rule_id: str,
    severity: Severity,
    category: str,
    json_path: str,
    message: str,
    evidence: str | None = None,
) -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=severity,
        category=category,
        json_path=json_path,
        message=message,
        evidence=evidence,
        source="deterministic",
    )


def iter_operations(spec: dict[str, Any]):
    """Yield ``(path, method, operation)`` for every documented operation."""
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            operation = item.get(method)
            if isinstance(operation, dict):
                yield path, method, operation


def op_path(path: str, method: str) -> str:
    return f"$.paths['{path}'].{method}"


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


@rule
def structural_schema(spec: dict[str, Any]) -> list[Finding]:
    """Validate the document's shape against the structural meta-schema."""
    validator = Draft202012Validator(OPENAPI_STRUCTURE)
    findings: list[Finding] = []
    for error in sorted(validator.iter_errors(spec), key=lambda e: list(e.absolute_path)):
        location = "$" + "".join(f"[{part!r}]" for part in error.absolute_path)
        findings.append(
            _finding(
                "struct.schema_violation",
                "critical",
                "structure",
                location,
                f"The document violates the OpenAPI structural contract at {location}: "
                f"{error.message}",
                str(error.validator_value)[:200],
            )
        )
    return findings[:10]


@rule
def has_operations(spec: dict[str, Any]) -> list[Finding]:
    if not list(iter_operations(spec)):
        return [
            _finding(
                "struct.no_operations",
                "critical",
                "structure",
                "$.paths",
                "The specification declares no operations, so there is nothing to integrate "
                "against.",
            )
        ]
    return []


@rule
def servers_declared(spec: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    servers = spec.get("servers") or []
    if not servers:
        findings.append(
            _finding(
                "srv.none_declared",
                "major",
                "usability",
                "$.servers",
                "No server URL is declared, so a reader cannot tell where to send requests.",
            )
        )
    for index, server in enumerate(servers):
        url = (server or {}).get("url", "") if isinstance(server, dict) else ""
        if urlparse(str(url)).scheme == "http":
            findings.append(
                _finding(
                    "srv.plaintext",
                    "major",
                    "security",
                    f"$.servers[{index}].url",
                    f"Server {url!r} is declared over plaintext HTTP; credentials sent to it "
                    "are exposed in transit.",
                    str(url)[:200],
                )
            )
    return findings


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


@rule
def operation_ids(spec: dict[str, Any]) -> list[Finding]:
    """Missing ids break codegen; duplicated ids break it more confusingly."""
    findings: list[Finding] = []
    seen: dict[str, str] = {}
    for path, method, operation in iter_operations(spec):
        location = op_path(path, method)
        op_id = operation.get("operationId")
        if not op_id:
            findings.append(
                _finding(
                    "op.missing_operation_id",
                    "major",
                    "completeness",
                    location,
                    f"{method.upper()} {path} has no operationId, so generated clients will "
                    "name this method from the path and break on any path change.",
                )
            )
            continue
        if op_id in seen:
            findings.append(
                _finding(
                    "op.duplicate_operation_id",
                    "major",
                    "completeness",
                    location,
                    f"operationId {op_id!r} is already used by {seen[op_id]}; codegen will "
                    "either collide or silently drop one of them.",
                    str(op_id),
                )
            )
        else:
            seen[op_id] = f"{method.upper()} {path}"
    return findings


@rule
def operation_descriptions(spec: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for path, method, operation in iter_operations(spec):
        if not (operation.get("summary") or operation.get("description")):
            findings.append(
                _finding(
                    "op.no_documentation",
                    "major",
                    "clarity",
                    op_path(path, method),
                    f"{method.upper()} {path} has neither a summary nor a description.",
                )
            )
    return findings


@rule
def error_responses(spec: dict[str, Any]) -> list[Finding]:
    """An endpoint that only documents success is documentation for the happy path only."""
    findings: list[Finding] = []
    for path, method, operation in iter_operations(spec):
        codes = [str(c) for c in (operation.get("responses") or {})]
        if not codes:
            findings.append(
                _finding(
                    "resp.none_documented",
                    "critical",
                    "completeness",
                    op_path(path, method) + ".responses",
                    f"{method.upper()} {path} documents no responses at all.",
                )
            )
            continue
        if not any(c.startswith(("4", "5")) or c == "default" for c in codes):
            findings.append(
                _finding(
                    "resp.no_error_documented",
                    "major",
                    "completeness",
                    op_path(path, method) + ".responses",
                    f"{method.upper()} {path} documents only {', '.join(sorted(codes))}; a "
                    "caller has no way to know what failures look like or how to handle them.",
                    ", ".join(sorted(codes)),
                )
            )
    return findings


@rule
def response_schemas(spec: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for path, method, operation in iter_operations(spec):
        for code, response in (operation.get("responses") or {}).items():
            if not isinstance(response, dict):
                continue
            location = f"{op_path(path, method)}.responses['{code}']"
            if not response.get("description"):
                findings.append(
                    _finding(
                        "resp.no_description",
                        "minor",
                        "completeness",
                        location,
                        f"{method.upper()} {path} response {code} has no description.",
                    )
                )
            for media_type, media in (response.get("content") or {}).items():
                if isinstance(media, dict) and not media.get("schema"):
                    findings.append(
                        _finding(
                            "resp.no_schema",
                            "major",
                            "completeness",
                            f"{location}.content['{media_type}']",
                            f"{method.upper()} {path} returns {media_type} for {code} but "
                            "declares no schema, so the payload shape is undocumented.",
                        )
                    )
    return findings


@rule
def request_bodies(spec: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for path, method, operation in iter_operations(spec):
        body = operation.get("requestBody")
        if not isinstance(body, dict):
            continue
        content = body.get("content") or {}
        if not content:
            findings.append(
                _finding(
                    "req.no_content",
                    "major",
                    "completeness",
                    op_path(path, method) + ".requestBody",
                    f"{method.upper()} {path} declares a request body with no content types.",
                )
            )
        for media_type, media in content.items():
            if isinstance(media, dict) and not media.get("schema"):
                findings.append(
                    _finding(
                        "req.no_schema",
                        "major",
                        "completeness",
                        f"{op_path(path, method)}.requestBody.content['{media_type}']",
                        f"{method.upper()} {path} accepts {media_type} but declares no schema "
                        "for it, so a caller cannot construct a valid request.",
                    )
                )
    return findings


@rule
def parameters(spec: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for path, method, operation in iter_operations(spec):
        for index, param in enumerate(operation.get("parameters") or []):
            if not isinstance(param, dict) or "$ref" in param:
                continue
            name = param.get("name", f"#{index}")
            location = f"{op_path(path, method)}.parameters[{index}]"
            if not param.get("schema") and not param.get("content"):
                findings.append(
                    _finding(
                        "param.no_schema",
                        "major",
                        "completeness",
                        location,
                        f"Parameter {name!r} on {method.upper()} {path} declares no type.",
                    )
                )
            if not param.get("description"):
                findings.append(
                    _finding(
                        "param.no_description",
                        "minor",
                        "clarity",
                        location,
                        f"Parameter {name!r} on {method.upper()} {path} is undocumented.",
                    )
                )
            if param.get("in") == "path" and param.get("required") is not True:
                findings.append(
                    _finding(
                        "param.path_not_required",
                        "major",
                        "structure",
                        location,
                        f"Path parameter {name!r} on {method.upper()} {path} is not marked "
                        "required, which the specification forbids.",
                    )
                )
    return findings


@rule
def examples_present(spec: dict[str, Any]) -> list[Finding]:
    """One finding for the whole document — per-operation would drown the report."""
    documented = 0
    total = 0
    for _path, _method, operation in iter_operations(spec):
        total += 1
        blob = str(operation.get("responses", "")) + str(operation.get("requestBody", ""))
        if "'example'" in blob or "'examples'" in blob:
            documented += 1
    if total and documented == 0:
        return [
            _finding(
                "example.none_anywhere",
                "minor",
                "usability",
                "$.paths",
                f"None of the {total} operations carry request or response examples, which is "
                "the single fastest way for an integrator to confirm they got the shape right.",
            )
        ]
    return []


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------


@rule
def security_schemes(spec: dict[str, Any]) -> list[Finding]:
    schemes = ((spec.get("components") or {}).get("securitySchemes") or {})
    global_security = spec.get("security") or []
    op_security = [
        (path, method)
        for path, method, operation in iter_operations(spec)
        if operation.get("security")
    ]

    if not schemes:
        return [
            _finding(
                "sec.no_scheme",
                "major",
                "security",
                "$.components.securitySchemes",
                "No security scheme is declared. Either the API is unauthenticated — which "
                "should be stated explicitly — or authentication is undocumented.",
            )
        ]
    if not global_security and not op_security:
        return [
            _finding(
                "sec.scheme_never_applied",
                "major",
                "security",
                "$.security",
                f"Security scheme(s) {', '.join(sorted(schemes))} are declared but never "
                "applied by a global or per-operation `security` block, so no operation is "
                "documented as requiring authentication.",
                ", ".join(sorted(schemes)),
            )
        ]
    return []


@rule
def enum_defaults(spec: dict[str, Any]) -> list[Finding]:
    """Enums without a default are the classic source of breaking additive changes."""
    findings: list[Finding] = []
    schemas = ((spec.get("components") or {}).get("schemas") or {})
    for schema_name, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        for prop_name, prop in (schema.get("properties") or {}).items():
            if not isinstance(prop, dict):
                continue
            if prop.get("enum") and "default" not in prop:
                findings.append(
                    _finding(
                        "schema.enum_without_default",
                        "minor",
                        "compatibility",
                        f"$.components.schemas['{schema_name}'].properties['{prop_name}']",
                        f"{schema_name}.{prop_name} is an enum with no declared default, so "
                        "adding a value later is a breaking change for strict clients.",
                        ", ".join(str(v) for v in prop["enum"][:6]),
                    )
                )
    return findings


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def run_all(spec: dict[str, Any]) -> list[Finding]:
    """Run every registered rule. A failing rule degrades to one finding, never a crash."""
    findings: list[Finding] = []
    for func in _RULES:
        try:
            findings.extend(func(spec))
        except Exception as exc:  # a malformed spec must not take the run down
            findings.append(
                _finding(
                    f"internal.rule_error.{func.__name__}",
                    "info",
                    "internal",
                    "$",
                    f"Rule {func.__name__!r} could not complete on this document: {exc}",
                )
            )
    return findings


def rule_count() -> int:
    return len(_RULES)


def score(findings: list[Finding]) -> dict[str, Any]:
    """Turn findings into a 0-100 score plus a per-category breakdown.

    Computed, never generated. Asking a model to produce the number would make
    two runs over an identical spec disagree, which is the one thing an
    evaluation tool cannot do.
    """
    counts: dict[str, int] = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    categories: dict[str, int] = {}
    penalty = 0

    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
        weight = SEVERITY_WEIGHT[finding.severity]
        penalty += weight
        categories[finding.category] = categories.get(finding.category, 0) + weight

    value = max(0, min(100, 100 - penalty))
    grade = (
        "A" if value >= 90 else
        "B" if value >= 75 else
        "C" if value >= 60 else
        "D" if value >= 40 else
        "F"
    )
    dominant = max(categories.items(), key=lambda kv: kv[1])[0] if categories else "none"

    return {
        "score": value,
        "grade": grade,
        "penalty": penalty,
        "severity_counts": counts,
        "category_penalties": dict(sorted(categories.items(), key=lambda kv: -kv[1])),
        "dominant_category": dominant,
        "finding_count": len(findings),
    }
