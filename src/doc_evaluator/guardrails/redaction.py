"""Output-side guardrail: strip credentials out of fetched content.

This runs on the boundary where *untrusted external text enters the model's
context*. Two distinct reasons, both worth stating plainly:

1. A published spec or a misconfigured server can leak a live key in an
   ``example`` block. Once that text is in a prompt it is in the request body,
   in any trace exporter, and in the run ledger. Redacting at the boundary is
   the only place it is cheap to do.
2. Redacted text is also what the *secret-egress* predicate inspects, so the
   same pattern set decides both "don't show the model this" and "don't let
   the model send this out". Keeping one table means the two can't drift.
"""

from __future__ import annotations

import re

# (name, compiled pattern). Ordered most-specific first so that a provider key
# is labelled as such rather than caught by the generic assignment rule.
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("bearer_header", re.compile(r"(?i)\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._\-]{20,}")),
    (
        "assigned_credential",
        re.compile(
            r"(?i)\b(api[_\-]?key|secret[_\-]?key|access[_\-]?token|client[_\-]?secret|password)"
            r"\b\s*[:=]\s*[\"']?([A-Za-z0-9._\-]{12,})[\"']?"
        ),
    ),
]

EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")

# Values that appear in every other public OpenAPI example. Flagging them as
# live credentials would train the operator to click through the confirmation
# dialog, which is worse than not having one.
PLACEHOLDER_TOKENS = frozenset(
    {
        "your_api_key",
        "your-api-key",
        "yourapikey",
        "changeme",
        "placeholder",
        "example",
        "redacted",
        "xxxxxxxxxxxx",
        "string",
        "<token>",
        "todo",
    }
)


def _is_placeholder(fragment: str) -> bool:
    stripped = fragment.strip("\"'<> ").lower()
    if stripped in PLACEHOLDER_TOKENS:
        return True
    # A run of identical characters is a mask, not a key.
    core = re.sub(r"[^A-Za-z0-9]", "", stripped)
    return bool(core) and len(set(core)) <= 2


def find_secrets(text: str) -> list[tuple[str, str]]:
    """Return ``(pattern_name, matched_text)`` for every credential-looking span."""
    if not text:
        return []
    hits: list[tuple[str, str]] = []
    for name, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            fragment = match.group(0)
            candidate = match.group(2) if pattern.groups >= 2 else fragment
            if _is_placeholder(candidate):
                continue
            hits.append((name, fragment))
    return hits


def contains_secret(text: str) -> bool:
    return bool(find_secrets(text))


def scrub(text: str, redact_emails: bool = True) -> tuple[str, list[str]]:
    """Return ``(clean_text, labels)``. Labels name what was removed, not its value."""
    if not text:
        return text, []

    labels: list[str] = []

    def _replace(name: str):
        def inner(match: re.Match[str]) -> str:
            candidate = match.group(2) if match.re.groups >= 2 else match.group(0)
            if _is_placeholder(candidate):
                return match.group(0)
            labels.append(name)
            if match.re.groups >= 2:
                # Preserve the key name so the model still sees the shape.
                return match.group(0).replace(match.group(2), f"[REDACTED:{name}]")
            return f"[REDACTED:{name}]"

        return inner

    clean = text
    for name, pattern in SECRET_PATTERNS:
        clean = pattern.sub(_replace(name), clean)

    if redact_emails:
        def _email(match: re.Match[str]) -> str:
            labels.append("email")
            return "[REDACTED:email]"

        clean = EMAIL_PATTERN.sub(_email, clean)

    return clean, sorted(set(labels))
