"""Fetcher: retrieve the target document and parse it into a spec object.

This is the boundary where untrusted external content enters the system, so it
carries two responsibilities beyond fetching: the fetch itself goes through the
guarded registry (an SSRF target is blocked here, not later), and the retrieved
text is redacted before any of it reaches a prompt.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..guardrails.redaction import scrub
from ..observability.instrument import timed
from ..state import EvalState
from ..tools.registry import ToolRegistry
from ..validation import iter_operations


def _digest_operations(spec: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    """A compact per-operation view for the validator's prompt.

    The full specification can be megabytes. What the qualitative pass actually
    needs is the prose and the shape, so that is all that gets sent.
    """
    digest = []
    for path, method, operation in iter_operations(spec):
        digest.append(
            {
                "path": path,
                "method": method,
                "operation_id": operation.get("operationId"),
                "summary": (operation.get("summary") or "")[:300],
                "description": (operation.get("description") or "")[:600],
                "response_codes": sorted(str(c) for c in (operation.get("responses") or {})),
                "has_request_body": bool(operation.get("requestBody")),
                "parameter_count": len(operation.get("parameters") or []),
            }
        )
        if len(digest) >= limit:
            break
    return digest


def make_fetcher(settings: Settings, registry: ToolRegistry):
    def fetcher(state: EvalState) -> dict[str, Any]:
        with timed("node:fetcher", "node"):
            target = state.get("target", "")

            fetched = registry.call("fetch_document", url=target)
            if not fetched.ok:
                return {
                    "completed": ["fetcher"],
                    "artifacts": {
                        **(state.get("artifacts") or {}),
                        "last_error": fetched.error,
                        "refused": fetched.refused,
                        "refusal": fetched.value if fetched.refused else None,
                    },
                    "guardrail_log": [fetched.event] if fetched.event else [],
                }

            raw_text = fetched.value["text"]
            # Redact before anything downstream can put this in a prompt.
            clean_text, redacted_labels = scrub(raw_text)

            parsed = registry.call("parse_openapi", text=clean_text)
            events = [e for e in (fetched.event, parsed.event) if e]

            if not parsed.ok:
                return {
                    "completed": ["fetcher"],
                    "artifacts": {
                        **(state.get("artifacts") or {}),
                        "source": fetched.value.get("source"),
                        "last_error": parsed.error,
                    },
                    "guardrail_log": events,
                }

            spec = parsed.value["spec"]
            operations = _digest_operations(spec)
            schemes = ((spec.get("components") or {}).get("securitySchemes") or {})

            return {
                "completed": ["fetcher"],
                "artifacts": {
                    **(state.get("artifacts") or {}),
                    "spec": spec,
                    "source": fetched.value.get("source"),
                    "transport": fetched.value.get("transport"),
                    "format": parsed.value.get("format"),
                    "bytes": fetched.value.get("bytes"),
                    "redacted": redacted_labels,
                    "operations": operations,
                    "operation_count": len(list(iter_operations(spec))),
                    "title": (spec.get("info") or {}).get("title") or "Untitled API",
                    "has_auth_scheme": bool(schemes),
                    "last_error": None,
                },
                "guardrail_log": events,
            }

    return fetcher
