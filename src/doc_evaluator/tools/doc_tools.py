"""Read-only tools: retrieve a document, parse it, validate it.

None of these are LangChain tools bound to a model. Workers call them directly
through the guarded registry. That is a deliberate narrowing: the model decides
*which worker runs*, and the worker decides *which tool runs with which
arguments*. Letting the model author tool arguments directly would put a
prompt-injected instruction in a fetched document one step away from the
`webhook_url` of an outbound POST.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..config import Settings

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024


class ToolError(RuntimeError):
    """A tool failed in a way the agent should hear about."""


def fetch_document(url: str, settings: Settings | None = None, **_: Any) -> dict[str, Any]:
    """Retrieve a spec from a URL or a local path.

    Local paths are supported so the project runs offline against its fixtures.
    They still pass through the guardrail engine — a path is an argument like
    any other, and exempting a code path from policy is how policies get holes.
    """
    settings = settings or Settings.from_env()

    if "://" not in url or url.startswith("file://"):
        path = Path(url.removeprefix("file://"))
        if not path.is_file():
            raise ToolError(f"no such document: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return {
            "status": "ok",
            "source": str(path),
            "transport": "file",
            "bytes": len(text.encode()),
            "text": text,
        }

    last_error: Exception | None = None
    for attempt in range(settings.max_retries):
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                response = client.get(url, headers={"Accept": "application/json, text/yaml, */*"})
                response.raise_for_status()
                text = response.text
                if len(text.encode()) > MAX_DOCUMENT_BYTES:
                    raise ToolError(
                        f"document is {len(text.encode()):,} bytes, over the "
                        f"{MAX_DOCUMENT_BYTES:,} byte ceiling"
                    )
                return {
                    "status": "ok",
                    "source": url,
                    "transport": "http",
                    "http_status": response.status_code,
                    "bytes": len(text.encode()),
                    "text": text,
                }
        except (httpx.HTTPError, httpx.StreamError) as exc:
            # Exponential backoff on transient transport failures only; a 404
            # is raised by raise_for_status as HTTPStatusError and retried once
            # more, which is cheap and occasionally correct behind a CDN.
            last_error = exc
            if attempt < settings.max_retries - 1:
                time.sleep(settings.backoff_base_seconds * (2**attempt))

    raise ToolError(f"could not fetch {url} after {settings.max_retries} attempts: {last_error}")


def parse_openapi(text: str, **_: Any) -> dict[str, Any]:
    """Parse JSON or YAML into a spec object. Pure and local."""
    if not (text or "").strip():
        raise ToolError("document is empty")

    spec: Any = None
    errors: list[str] = []
    for name, loader in (("json", json.loads), ("yaml", yaml.safe_load)):
        try:
            spec = loader(text)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        if isinstance(spec, dict):
            return {"status": "ok", "format": name, "spec": spec}
        errors.append(f"{name}: parsed to {type(spec).__name__}, expected an object")

    raise ToolError("document is neither JSON nor YAML — " + "; ".join(errors[:2]))


def validate_spec(spec: dict[str, Any], **_: Any) -> dict[str, Any]:
    """Run the deterministic rule set. Imported lazily to keep the tool module light."""
    from ..validation import rule_count, run_all, score

    findings = run_all(spec)
    return {
        "status": "ok",
        "rules_run": rule_count(),
        "findings": [f.model_dump() for f in findings],
        "score": score(findings),
    }
