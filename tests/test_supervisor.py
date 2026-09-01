"""Supervisor routing: the model proposes, the preconditions dispose."""

from __future__ import annotations

import pytest

from doc_evaluator.agents.supervisor import _apply_preconditions, _facts
from doc_evaluator.guardrails.budget import BudgetGuard
from doc_evaluator.observability.ledger import RunLedger


def facts(**over):
    base = {"completed": [], "has_artifact": False, "has_report": False, "finding_count": 0}
    base.update(over)
    return base


class TestPreconditions:
    def test_validation_without_a_spec_is_redirected_to_fetch(self):
        route, override = _apply_preconditions("validator", facts())
        assert route == "fetcher" and override

    def test_reporting_without_validation_is_redirected(self):
        route, override = _apply_preconditions(
            "reporter", facts(has_artifact=True, completed=["fetcher"])
        )
        assert route == "validator" and override

    def test_refetching_an_existing_artifact_is_redirected(self):
        """The classic supervisor loop: re-dispatching a worker that already ran."""
        route, override = _apply_preconditions(
            "fetcher", facts(has_artifact=True, completed=["fetcher"])
        )
        assert route == "validator" and override

    def test_revalidating_is_redirected_forward(self):
        route, _ = _apply_preconditions(
            "validator", facts(has_artifact=True, completed=["fetcher", "validator"])
        )
        assert route == "reporter"

    def test_finishing_early_is_redirected_to_the_missing_step(self):
        assert _apply_preconditions("finish", facts())[0] == "fetcher"
        assert _apply_preconditions("finish", facts(has_artifact=True))[0] == "validator"
        assert (
            _apply_preconditions(
                "finish", facts(has_artifact=True, completed=["fetcher", "validator"])
            )[0]
            == "reporter"
        )

    def test_a_legal_move_is_left_alone(self):
        route, override = _apply_preconditions(
            "reporter", facts(has_artifact=True, completed=["fetcher", "validator"])
        )
        assert route == "reporter" and override is None

    def test_finishing_with_a_report_is_legal(self):
        route, override = _apply_preconditions(
            "finish",
            facts(has_artifact=True, has_report=True, completed=["fetcher", "validator"]),
        )
        assert route == "finish" and override is None

    @pytest.mark.parametrize("proposal", ["fetcher", "validator", "reporter", "finish"])
    def test_every_proposal_resolves_to_a_legal_route(self, proposal):
        for state in [
            facts(),
            facts(has_artifact=True),
            facts(has_artifact=True, completed=["fetcher", "validator"]),
            facts(has_artifact=True, has_report=True, completed=["fetcher", "validator"]),
        ]:
            route, _ = _apply_preconditions(proposal, state)
            assert route in {"fetcher", "validator", "reporter", "finish"}


class TestFacts:
    def test_facts_summarise_state_without_the_whole_spec(self):
        state = {
            "artifacts": {"spec": {"openapi": "3.0.0"}, "operation_count": 7, "source": "x.json"},
            "findings": [1, 2, 3],
            "completed": ["fetcher"],
            "iteration": 2,
        }
        result = _facts(state)
        assert result["has_artifact"] is True
        assert result["finding_count"] == 3
        assert "spec" not in result, "the routing prompt must not carry the whole document"


class TestBudget:
    def test_iteration_ceiling_trips(self):
        assert BudgetGuard(max_iterations=3).check(3).exceeded

    def test_below_the_ceiling_is_fine(self):
        assert not BudgetGuard(max_iterations=3).check(2).exceeded

    def test_token_ceiling_trips(self):
        ledger = RunLedger()
        ledger.record_llm("x", 1.0, 900, 200)
        status = BudgetGuard(max_total_tokens=1000).check(0, ledger)
        assert status.exceeded and status.limit == "max_total_tokens"

    def test_wall_clock_ceiling_trips(self):
        status = BudgetGuard(max_wall_seconds=0.0).check(0, RunLedger())
        assert status.exceeded and status.limit == "max_wall_seconds"

    def test_reason_names_the_actual_numbers(self):
        reason = BudgetGuard(max_iterations=5).check(5).reason
        assert "5" in reason
