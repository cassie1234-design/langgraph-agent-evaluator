"""Deterministic rule set and scoring."""

from __future__ import annotations

import pytest

from doc_evaluator.state import Finding
from doc_evaluator.validation import iter_operations, rule_count, run_all, score


def rule_ids(spec: dict) -> set[str]:
    return {f.rule_id for f in run_all(spec)}


class TestCleanSpecification:
    def test_well_formed_spec_produces_no_findings(self, good_spec):
        assert run_all(good_spec) == []

    def test_clean_spec_scores_full_marks(self, good_spec):
        result = score(run_all(good_spec))
        assert result["score"] == 100 and result["grade"] == "A"


class TestDefectDetection:
    def test_duplicate_operation_id_is_caught(self, bad_spec):
        assert "op.duplicate_operation_id" in rule_ids(bad_spec)

    def test_missing_operation_id_is_caught(self, bad_spec):
        assert "op.missing_operation_id" in rule_ids(bad_spec)

    def test_undocumented_errors_are_caught(self, bad_spec):
        assert "resp.no_error_documented" in rule_ids(bad_spec)

    def test_plaintext_server_is_caught(self, bad_spec):
        assert "srv.plaintext" in rule_ids(bad_spec)

    def test_declared_but_unapplied_security_scheme_is_caught(self, bad_spec):
        assert "sec.scheme_never_applied" in rule_ids(bad_spec)

    def test_schemaless_request_body_is_caught(self, bad_spec):
        assert "req.no_schema" in rule_ids(bad_spec)

    def test_path_parameter_not_marked_required_is_caught(self, bad_spec):
        assert "param.path_not_required" in rule_ids(bad_spec)

    def test_enum_without_default_is_caught(self, bad_spec):
        assert "schema.enum_without_default" in rule_ids(bad_spec)

    def test_structural_violations_are_critical(self, broken_spec):
        findings = [f for f in run_all(broken_spec) if f.rule_id == "struct.schema_violation"]
        assert findings and all(f.severity == "critical" for f in findings)

    def test_operation_with_no_responses_is_critical(self, broken_spec):
        assert "resp.none_documented" in rule_ids(broken_spec)


class TestRobustness:
    @pytest.mark.parametrize(
        "spec",
        [
            {},
            {"openapi": "3.0.0"},
            {"openapi": "3.0.0", "info": {}, "paths": None},
            {"openapi": "3.0.0", "info": {"title": "t", "version": "1"}, "paths": {}},
            {"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
             "paths": {"/x": {"get": None}}},
            {"openapi": "3.0.0", "info": {"title": "t", "version": "1"},
             "paths": {"/x": {"get": {"responses": {"200": "not-an-object"}}}}},
        ],
    )
    def test_malformed_documents_never_raise(self, spec):
        findings = run_all(spec)
        assert isinstance(findings, list)
        assert not any(f.rule_id.startswith("internal.rule_error") for f in findings), (
            "a rule crashed on a malformed document"
        )

    def test_every_finding_carries_an_actionable_message(self, bad_spec):
        for finding in run_all(bad_spec):
            assert len(finding.message) > 30, f"{finding.rule_id} message is too thin"
            assert finding.json_path.startswith("$")

    def test_rules_are_registered(self):
        assert rule_count() >= 10

    def test_results_are_reproducible(self, bad_spec):
        assert [f.rule_id for f in run_all(bad_spec)] == [f.rule_id for f in run_all(bad_spec)]

    def test_iter_operations_finds_every_method(self, bad_spec):
        found = {(p, m) for p, m, _ in iter_operations(bad_spec)}
        assert ("/invoices", "get") in found and ("/invoices/{id}", "delete") in found


class TestScoring:
    def test_score_is_bounded(self):
        many = [Finding(rule_id=f"r{i}", severity="critical", message="m" * 40) for i in range(50)]
        assert score(many)["score"] == 0

    def test_no_findings_is_a_perfect_score(self):
        assert score([])["score"] == 100

    def test_critical_costs_more_than_minor(self):
        critical = score([Finding(rule_id="a", severity="critical", message="m" * 40)])
        minor = score([Finding(rule_id="b", severity="minor", message="m" * 40)])
        assert critical["score"] < minor["score"]

    def test_info_findings_do_not_reduce_the_score(self):
        assert score([Finding(rule_id="a", severity="info", message="m" * 40)])["score"] == 100

    def test_dominant_category_reflects_the_heaviest_penalty(self):
        result = score(
            [
                Finding(rule_id="a", severity="critical", category="security", message="m" * 40),
                Finding(rule_id="b", severity="minor", category="clarity", message="m" * 40),
            ]
        )
        assert result["dominant_category"] == "security"

    @pytest.mark.parametrize(
        ("penalty_findings", "expected_grade"),
        # A major finding costs 15 points: 100, 85, 70, 55, 25.
        [(0, "A"), (1, "B"), (2, "C"), (3, "D"), (5, "F")],
    )
    def test_grade_boundaries(self, penalty_findings, expected_grade):
        findings = [
            Finding(rule_id=f"r{i}", severity="major", message="m" * 40)
            for i in range(penalty_findings)
        ]
        assert score(findings)["grade"] == expected_grade
