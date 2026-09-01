"""Policy engine: tiers, escalation and the enforcement contract."""

from __future__ import annotations

import pytest

from doc_evaluator.guardrails.engine import (
    Action,
    GuardrailEngine,
    PolicyError,
    Tier,
    escalate,
    load_policy,
)
from doc_evaluator.guardrails.predicates import UnknownPredicateError
from doc_evaluator.samples import SYNTHETIC_CREDENTIAL


class TestTierOrdering:
    def test_ranks_are_strictly_increasing(self):
        ranks = [Tier.SAFE.rank, Tier.SENSITIVE.rank, Tier.HIGH_RISK.rank, Tier.FORBIDDEN.rank]
        assert ranks == sorted(ranks) and len(set(ranks)) == 4

    @pytest.mark.parametrize(
        ("current", "candidate", "expected"),
        [
            (Tier.SAFE, Tier.FORBIDDEN, Tier.FORBIDDEN),
            (Tier.FORBIDDEN, Tier.SAFE, Tier.FORBIDDEN),
            (Tier.SENSITIVE, Tier.HIGH_RISK, Tier.HIGH_RISK),
            (Tier.HIGH_RISK, Tier.SENSITIVE, Tier.HIGH_RISK),
            (Tier.SAFE, Tier.SAFE, Tier.SAFE),
        ],
    )
    def test_escalate_only_raises(self, current, candidate, expected):
        assert escalate(current, candidate) is expected

    def test_escalation_does_not_use_string_comparison(self):
        """Regression guard.

        ``Tier`` subclasses ``str``, so ``max(Tier.SAFE, Tier.FORBIDDEN)`` compares
        alphabetically and returns SAFE — every escalation silently no-ops while
        the policy file still looks correct. This asserts the trap directly so a
        future refactor back to ``max()`` fails loudly.
        """
        assert max(Tier.SAFE, Tier.FORBIDDEN) is Tier.SAFE  # the trap
        assert escalate(Tier.SAFE, Tier.FORBIDDEN) is Tier.FORBIDDEN  # the fix


class TestPolicyLoading:
    def test_shipped_policy_loads(self):
        policy = load_policy()
        assert policy["tools"], "the shipped policy declares no tools"
        assert policy["default_tier"] is Tier.SENSITIVE

    def test_unknown_predicate_fails_at_load_time(self, tmp_path):
        bad = tmp_path / "policy.yaml"
        bad.write_text(
            "version: 1\ndefault_tier: SENSITIVE\ntools:\n"
            "  t:\n    tier: SAFE\n    rules:\n"
            "      - id: r\n        arg: a\n        predicate: no_such_predicate\n"
            "        escalate_to: FORBIDDEN\n"
        )
        with pytest.raises(UnknownPredicateError):
            load_policy(bad)

    def test_invalid_tier_fails_at_load_time(self, tmp_path):
        bad = tmp_path / "policy.yaml"
        bad.write_text("version: 1\ntools:\n  t:\n    tier: MOSTLY_FINE\n")
        with pytest.raises(PolicyError):
            load_policy(bad)

    def test_incomplete_rule_fails_at_load_time(self, tmp_path):
        bad = tmp_path / "policy.yaml"
        bad.write_text(
            "version: 1\ntools:\n  t:\n    tier: SAFE\n    rules:\n      - id: r\n        arg: a\n"
        )
        with pytest.raises(PolicyError):
            load_policy(bad)


class TestEvaluation:
    def test_allowlisted_fetch_is_safe(self, engine):
        d = engine.evaluate("fetch_document", {"url": "https://petstore3.swagger.io/spec.json"})
        assert (d.tier, d.action) == (Tier.SAFE, Action.ALLOW)
        assert d.allowed and not d.needs_approval and not d.blocked

    def test_subdomain_of_allowlisted_host_is_safe(self, engine):
        d = engine.evaluate("fetch_document", {"url": "https://a.raw.githubusercontent.com/s"})
        assert d.tier is Tier.SAFE

    def test_off_allowlist_fetch_is_audited_not_blocked(self, engine):
        d = engine.evaluate("fetch_document", {"url": "https://cdn.vendor.example/spec.json"})
        assert (d.tier, d.action) == (Tier.SENSITIVE, Action.ALLOW_AUDITED)
        assert "fetch.off_allowlist" in d.matched_rules

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:8080/admin",
            "https://10.0.0.5/internal",
            "http://192.168.1.1/",
            "https://metadata.google.internal/computeMetadata/v1/",
        ],
    )
    def test_ssrf_targets_are_forbidden(self, engine, url):
        d = engine.evaluate("fetch_document", {"url": url})
        assert d.tier is Tier.FORBIDDEN and d.blocked
        assert "fetch.ssrf" in d.matched_rules

    def test_path_traversal_is_forbidden(self, engine):
        d = engine.evaluate("fetch_document", {"url": "/etc/passwd"})
        assert d.tier is Tier.FORBIDDEN
        assert "fetch.path_escape" in d.matched_rules

    def test_workspace_local_read_is_audited(self, engine):
        d = engine.evaluate("fetch_document", {"url": "fixtures/specs/petstore.json"})
        assert d.tier is Tier.SENSITIVE
        assert "fetch.local_read" in d.matched_rules
        assert "fetch.off_allowlist" not in d.matched_rules, "a path has no host to allowlist"

    def test_bounded_delete_requires_approval(self, engine):
        d = engine.evaluate("purge_cache", {"pattern": "specs/petstore.json"})
        assert (d.tier, d.action) == (Tier.HIGH_RISK, Action.CONFIRM)
        assert d.needs_approval

    @pytest.mark.parametrize("pattern", ["*", "**", "", "/", "all", "spec_*", "../*"])
    def test_unbounded_delete_is_forbidden(self, engine, pattern):
        d = engine.evaluate("purge_cache", {"pattern": pattern})
        assert d.tier is Tier.FORBIDDEN, f"{pattern!r} should not be approvable"

    def test_clean_egress_requires_approval(self, engine):
        d = engine.evaluate(
            "send_external_report",
            {"webhook_url": "https://petstore3.swagger.io/hook", "body": "Score 92/100."},
        )
        assert d.action is Action.CONFIRM

    def test_egress_carrying_a_credential_is_forbidden(self, engine):
        d = engine.evaluate(
            "send_external_report",
            {
                "webhook_url": "https://petstore3.swagger.io/hook",
                "body": f"use {SYNTHETIC_CREDENTIAL}",
            },
        )
        assert d.tier is Tier.FORBIDDEN
        assert "egress.secret_leak" in d.matched_rules

    def test_egress_to_unknown_destination_is_forbidden(self, engine):
        d = engine.evaluate(
            "send_external_report",
            {"webhook_url": "https://attacker.example/collect", "body": "clean"},
        )
        assert d.tier is Tier.FORBIDDEN
        assert "egress.unknown_destination" in d.matched_rules

    def test_undeclared_tool_gets_the_default_tier(self, engine):
        d = engine.evaluate("some_new_tool", {"x": 1})
        assert d.tier is Tier.SENSITIVE
        assert d.tier is not Tier.SAFE, "an unknown tool must not default to free"
        assert "policy.undeclared_tool" in d.matched_rules

    def test_rules_only_fire_for_arguments_that_are_present(self, engine):
        d = engine.evaluate("fetch_document", {"unrelated": "value"})
        assert d.tier is Tier.SAFE and not d.matched_rules

    def test_disabled_engine_labels_its_own_output(self, settings):
        disabled = GuardrailEngine(allowed_hosts=(), enabled=False)
        d = disabled.evaluate("purge_cache", {"pattern": "*"})
        assert d.action is Action.ALLOW
        assert "disabled" in d.reason, "a disabled decision must be distinguishable"

    def test_decision_records_evaluation_cost(self, engine):
        d = engine.evaluate("fetch_document", {"url": "https://petstore3.swagger.io/s.json"})
        assert d.eval_micros > 0

    def test_refusal_payload_is_structured_and_actionable(self, engine):
        d = engine.evaluate("purge_cache", {"pattern": "*"})
        payload = d.refusal_payload()
        assert payload["status"] == "refused"
        assert payload["risk_tier"] == "FORBIDDEN"
        assert payload["matched_rules"] and payload["guidance"]

    def test_identical_arguments_produce_identical_digests(self, engine):
        a = engine.evaluate("purge_cache", {"pattern": "x", "dry_run": True})
        b = engine.evaluate("purge_cache", {"dry_run": True, "pattern": "x"})
        assert a.args_digest == b.args_digest
