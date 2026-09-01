"""A machine-readable context block embedded in every prompt.

Each agent renders the facts it wants the model to reason over as JSON inside
``<eval_context>`` tags, rather than prose-ifying them. Two payoffs:

* the live model gets unambiguous, parseable state instead of a paraphrase; and
* the mock model can recover exactly the same facts, so replay mode reproduces
  real routing and real findings instead of a canned script.

The second point is what makes the no-API-key mode worth having: the graph,
guardrails, ledger and benchmark all exercise their real logic.
"""

from __future__ import annotations

import json
import re
from typing import Any

OPEN, CLOSE = "<eval_context>", "</eval_context>"
_BLOCK = re.compile(re.escape(OPEN) + r"\s*(.*?)\s*" + re.escape(CLOSE), re.DOTALL)


def render(payload: dict[str, Any]) -> str:
    return f"{OPEN}\n{json.dumps(payload, indent=2, default=str, sort_keys=True)}\n{CLOSE}"


def extract(text: str) -> dict[str, Any]:
    """Pull the last context block out of a prompt. Returns ``{}`` if absent."""
    blocks = _BLOCK.findall(text or "")
    for block in reversed(blocks):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}
