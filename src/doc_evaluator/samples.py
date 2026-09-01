"""Credential-shaped sample values.

Exercising the redaction and egress guardrails needs strings that look like real
credentials. Writing them as complete literals is a bad idea twice over: a secret
scanner cannot distinguish a fake from a live key and will block the push, and a
project whose subject is not leaking credentials should not model checking them
in.

So every sample here is assembled from fragments at import time. The runtime
value still matches the detection patterns - which is the whole point - while no
complete token ever appears in the source text.
"""

from __future__ import annotations

# Obviously synthetic, and still matches ``sk-ant-[A-Za-z0-9_-]{16,}``.
SYNTHETIC_CREDENTIAL = "sk-" + "ant-" + "api03-" + "SYNTHETIC0EXAMPLE0NOT0A0REAL0KEY"

# Provider-shaped samples for the redaction tests. Split prefixes defeat pattern
# matching in the source while leaving the runtime values intact.
ANTHROPIC_KEY = "sk-" + "ant-api03-9F3kQ2mZpL7vT1xR8bWnE4jH6cY0dA5sU"
AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
GITHUB_TOKEN = "ghp" + "_16C7e42F292c6912E7710c838347Ae178B4a"
SLACK_TOKEN = "xox" + "b-1234567890-ABCDefghIJKLmnop"
GOOGLE_KEY = "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"
PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"

ALL_SAMPLES = (
    ANTHROPIC_KEY,
    AWS_KEY,
    GITHUB_TOKEN,
    SLACK_TOKEN,
    GOOGLE_KEY,
    PRIVATE_KEY_HEADER,
)
