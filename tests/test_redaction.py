"""Content redaction: what gets stripped, and what must not be."""

from __future__ import annotations

import pytest

from doc_evaluator.guardrails.redaction import contains_secret, find_secrets, scrub
from doc_evaluator.samples import (
    ANTHROPIC_KEY,
    AWS_KEY,
    GITHUB_TOKEN,
    PRIVATE_KEY_HEADER,
    SLACK_TOKEN,
)

# Samples live in doc_evaluator.samples and are assembled from fragments there,
# so no complete credential literal appears anywhere in the repository.
REAL_SECRETS = [
    ANTHROPIC_KEY,
    AWS_KEY,
    GITHUB_TOKEN,
    SLACK_TOKEN,
    PRIVATE_KEY_HEADER,
]

PLACEHOLDERS = [
    "api_key: your_api_key",
    "api_key = changeme",
    "api_key: xxxxxxxxxxxxxxxx",
    "apiKey: <token>",
    "password: string",
]


class TestDetection:
    @pytest.mark.parametrize("secret", REAL_SECRETS)
    def test_real_credentials_are_detected(self, secret):
        assert contains_secret(f"Authorization uses {secret} here")

    @pytest.mark.parametrize("text", PLACEHOLDERS)
    def test_placeholders_are_not_flagged(self, text):
        """False positives are the failure mode that kills a guardrail.

        Every public OpenAPI example contains ``your_api_key``. If those trip the
        egress block, the operator learns the dialog is noise and starts clicking
        through it — which is strictly worse than not having the check.
        """
        assert not contains_secret(text), f"{text!r} is a placeholder, not a credential"

    def test_ordinary_prose_is_clean(self):
        assert not contains_secret(
            "Send the request with a bearer token obtained from the login endpoint."
        )

    def test_finds_report_the_pattern_name(self):
        hits = find_secrets(f"key {ANTHROPIC_KEY}")
        assert hits and hits[0][0] == "anthropic_key"


class TestScrubbing:
    def test_secret_value_is_removed_from_output(self):
        clean, labels = scrub(f"curl -H 'x-api-key: {ANTHROPIC_KEY}' https://api.example.com")
        assert ANTHROPIC_KEY not in clean
        assert "anthropic_key" in labels

    def test_key_name_survives_so_the_model_still_sees_the_shape(self):
        clean, _ = scrub("api_key: 9f8e7d6c5b4a32100011")
        assert "api_key" in clean and "9f8e7d6c5b4a32100011" not in clean

    def test_emails_are_redacted(self):
        clean, labels = scrub("contact billing-ops@example.com")
        assert "billing-ops@example.com" not in clean and "email" in labels

    def test_emails_can_be_kept(self):
        clean, labels = scrub("contact billing-ops@example.com", redact_emails=False)
        assert "billing-ops@example.com" in clean and "email" not in labels

    def test_labels_name_the_kind_never_the_value(self):
        _clean, labels = scrub(f"aws {AWS_KEY}")
        assert all(AWS_KEY not in label for label in labels)

    def test_clean_text_is_returned_unchanged(self):
        text = "A perfectly ordinary description of a REST endpoint."
        assert scrub(text) == (text, [])

    def test_empty_input_is_safe(self):
        assert scrub("") == ("", [])

    def test_scrubbing_is_idempotent(self):
        once, _ = scrub(f"key {ANTHROPIC_KEY}")
        twice, _ = scrub(once)
        assert once == twice
