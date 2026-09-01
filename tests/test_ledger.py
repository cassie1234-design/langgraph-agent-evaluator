"""Cost and latency accounting."""

from __future__ import annotations

import json

import pytest

from doc_evaluator.observability.instrument import get_ledger, timed, use_ledger
from doc_evaluator.observability.ledger import RunLedger, Span
from doc_evaluator.observability.pricing import PRICING, cost_usd, rate_for


class TestPricing:
    def test_known_model_prices_correctly(self):
        # 1M input at $5 + 1M output at $25.
        assert cost_usd("claude-opus-5", 1_000_000, 1_000_000) == pytest.approx(30.0)

    def test_cheaper_tiers_cost_less(self):
        assert cost_usd("claude-haiku-4-5", 10_000, 10_000) < cost_usd(
            "claude-opus-5", 10_000, 10_000
        )

    def test_unknown_model_falls_back_rather_than_reporting_zero(self):
        """Silently pricing an unknown model at $0 would make the panel misleading."""
        assert cost_usd("some-future-model", 1_000_000, 0) > 0
        assert rate_for("some-future-model") == PRICING["claude-opus-5"]

    def test_cache_reads_are_discounted(self):
        full = cost_usd("claude-opus-5", 100_000, 0)
        cached = cost_usd("claude-opus-5", 100_000, 0, cache_read_tokens=100_000)
        assert cached == pytest.approx(full * 0.10)

    def test_zero_usage_is_free(self):
        assert cost_usd("claude-opus-5") == 0.0


class TestLedger:
    def test_llm_spans_accumulate(self):
        ledger = RunLedger(model="claude-opus-5")
        ledger.record_llm("a", 10.0, 1000, 100)
        ledger.record_llm("b", 20.0, 2000, 200)
        assert ledger.total_tokens == 3300
        assert ledger.total_usd == pytest.approx(cost_usd("claude-opus-5", 3000, 300))

    def test_guardrail_time_is_isolated_from_everything_else(self):
        ledger = RunLedger()
        ledger.record(Span("guardrail:x", "guardrail", 2.0))
        ledger.record(Span("tool:x", "tool", 50.0))
        ledger.record(Span("node:x", "node", 90.0))
        assert ledger.guardrail_ms == pytest.approx(2.0)

    def test_summary_reports_each_span_kind(self):
        ledger = RunLedger()
        ledger.record_llm("a", 5.0, 100, 10)
        ledger.record(Span("tool:t", "tool", 1.0))
        ledger.record(Span("guardrail:t", "guardrail", 0.5))
        summary = ledger.summary()
        assert summary["llm_calls"] == 1
        assert summary["tool_calls"] == 1
        assert summary["guardrail_evaluations"] == 1

    def test_by_name_groups_repeated_calls(self):
        ledger = RunLedger()
        for _ in range(3):
            ledger.record(Span("tool:fetch", "tool", 1.0))
        assert ledger.by_name()["tool:fetch"]["calls"] == 3

    def test_json_export_round_trips(self):
        ledger = RunLedger()
        ledger.record_llm("a", 5.0, 100, 10)
        payload = json.loads(ledger.to_json())
        assert payload["summary"]["total_tokens"] == 110
        assert len(payload["spans"]) == 1

    def test_empty_ledger_is_well_formed(self):
        summary = RunLedger().summary()
        assert summary["total_usd"] == 0 and summary["guardrail_mean_ms"] == 0.0


class TestAmbientLedger:
    def test_timed_records_into_the_active_ledger(self):
        ledger = RunLedger()
        with use_ledger(ledger), timed("thing", "node"):
            pass
        assert len(ledger.spans) == 1 and ledger.spans[0].wall_ms > 0

    def test_timed_outside_a_context_is_a_no_op(self):
        with timed("thing", "node"):
            pass  # must not raise

    def test_context_is_restored_on_exit(self):
        outer = RunLedger()
        with use_ledger(outer):
            with use_ledger(RunLedger()):
                pass
            assert get_ledger() is outer
        assert get_ledger() is None

    def test_timing_survives_an_exception(self):
        ledger = RunLedger()
        with use_ledger(ledger):
            with pytest.raises(ValueError):
                with timed("boom", "tool"):
                    raise ValueError("x")
        assert len(ledger.spans) == 1, "a failed call must still be accounted for"
