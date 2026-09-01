"""Tools with real-world side effects.

These exist to make the guardrail layer demonstrable rather than theoretical.
Both are genuinely implemented — ``purge_cache`` really deletes, and
``send_external_report`` really POSTs — because a guardrail wrapped around a
no-op proves nothing. Both sit behind HIGH_RISK in the policy, so neither runs
without either an explicit human approval or a policy decision to block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from ..config import REPO_ROOT, Settings

CACHE_DIR = REPO_ROOT / ".doc_eval_cache"


def purge_cache(pattern: str, dry_run: bool = False, **_: Any) -> dict[str, Any]:
    """Delete cached artifacts matching ``pattern`` under the cache directory.

    The wildcard case never reaches this function — ``delete.unbounded_scope``
    blocks it at the policy layer. The resolved-path check below is a second,
    independent barrier: a policy is a statement of intent, and intent is not a
    substitute for the function refusing to delete outside its own directory.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    matches = [p for p in CACHE_DIR.glob(pattern) if p.is_file()]

    safe: list[Path] = []
    for path in matches:
        try:
            path.resolve().relative_to(CACHE_DIR.resolve())
        except ValueError:
            continue  # path traversal out of the cache dir
        safe.append(path)

    if dry_run:
        return {"status": "ok", "dry_run": True, "would_delete": [p.name for p in safe]}

    deleted = []
    for path in safe:
        path.unlink()
        deleted.append(path.name)
    return {"status": "ok", "deleted": deleted, "count": len(deleted)}


def send_external_report(
    webhook_url: str, body: str, settings: Settings | None = None, **_: Any
) -> dict[str, Any]:
    """POST the finished report to an outbound webhook."""
    settings = settings or Settings.from_env()
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                webhook_url,
                json={"source": "doc-evaluator", "report": body},
                headers={"Content-Type": "application/json"},
            )
        return {
            "status": "ok",
            "http_status": response.status_code,
            "destination": webhook_url,
            "bytes_sent": len(body.encode()),
        }
    except httpx.HTTPError as exc:
        return {"status": "error", "destination": webhook_url, "error": str(exc)}
