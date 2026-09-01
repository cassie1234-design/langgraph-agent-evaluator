"""The guarded registry: enforcement, approval and the refusal contract."""

from __future__ import annotations

from doc_evaluator.config import Settings
from doc_evaluator.guardrails.engine import Action, Tier
from doc_evaluator.observability.instrument import use_ledger
from doc_evaluator.observability.ledger import RunLedger
from doc_evaluator.tools.registry import ToolRegistry, deny_all_approver


def registry(settings: Settings, approver=deny_all_approver) -> ToolRegistry:
    return ToolRegistry(settings=settings, approver=approver)


class TestBlocking:
    def test_forbidden_call_never_executes(self, settings):
        executed = []
        reg = registry(settings)
        reg.tools["purge_cache"] = lambda **kw: executed.append(kw)

        result = reg.call("purge_cache", pattern="*")

        assert executed == [], "a FORBIDDEN call reached the implementation"
        assert result.refused and not result.ok

    def test_block_returns_data_not_an_exception(self, settings):
        """The agent has to be able to read why it was refused and route around it."""
        result = registry(settings).call("purge_cache", pattern="*")
        assert isinstance(result.value, dict)
        assert result.value["status"] == "refused"
        assert result.value["guidance"]

    def test_ssrf_fetch_is_blocked_before_any_network_call(self, settings):
        called = []
        reg = registry(settings)
        reg.tools["fetch_document"] = lambda **kw: called.append(kw)

        result = reg.call("fetch_document", url="http://169.254.169.254/latest/meta-data/")

        assert called == []
        assert result.decision.tier is Tier.FORBIDDEN


class TestApproval:
    def test_high_risk_call_is_refused_when_denied(self, settings):
        executed = []
        reg = registry(settings, approver=lambda d, a: False)
        reg.tools["purge_cache"] = lambda **kw: executed.append(kw)

        result = reg.call("purge_cache", pattern="specs/x.json")

        assert executed == []
        assert result.value["status"] == "denied_by_operator"
        assert result.event.approved is False

    def test_high_risk_call_executes_when_approved(self, settings):
        executed = []
        reg = registry(settings, approver=lambda d, a: True)
        reg.tools["purge_cache"] = lambda **kw: executed.append(kw) or {"ok": True}

        result = reg.call("purge_cache", pattern="specs/x.json")

        assert len(executed) == 1
        assert result.ok and result.event.approved is True

    def test_default_approver_denies(self, settings):
        """Unattended runs must not silently downgrade HIGH_RISK to SAFE."""
        assert deny_all_approver(None, {}) is False
        result = registry(settings).call("purge_cache", pattern="specs/x.json")
        assert not result.ok

    def test_approver_sees_the_decision_and_the_arguments(self, settings):
        seen = {}

        def approver(decision, args):
            seen["tier"] = decision.tier
            seen["args"] = args
            return False

        registry(settings, approver).call("purge_cache", pattern="specs/x.json")
        assert seen["tier"] is Tier.HIGH_RISK
        assert seen["args"]["pattern"] == "specs/x.json"

    def test_approval_is_not_requested_for_safe_calls(self, settings):
        asked = []
        reg = registry(settings, approver=lambda d, a: asked.append(d) or True)
        reg.tools["parse_openapi"] = lambda **kw: {"spec": {}}

        reg.call("parse_openapi", text="{}")
        assert asked == [], "a SAFE call must not interrupt anyone"


class TestExecution:
    def test_safe_call_runs_and_returns_its_value(self, settings, spec_path):
        result = registry(settings).call("fetch_document", url=str(spec_path))
        assert result.ok and result.value["status"] == "ok"
        assert "openapi" in result.value["text"]

    def test_tool_exception_is_captured_not_raised(self, settings):
        reg = registry(settings)

        def boom(**kw):
            raise ValueError("deliberate")

        reg.tools["parse_openapi"] = boom
        result = reg.call("parse_openapi", text="{}")
        assert not result.ok and "deliberate" in result.error

    def test_missing_implementation_is_reported(self, settings):
        reg = registry(settings)
        reg.tools.pop("validate_spec")
        result = reg.call("validate_spec", spec={})
        assert not result.ok and "no implementation" in result.error

    def test_every_call_produces_an_audit_event(self, settings, spec_path):
        result = registry(settings).call("fetch_document", url=str(spec_path))
        assert result.event is not None
        assert result.event.tool == "fetch_document"
        assert result.event.args_digest


class TestInstrumentation:
    def test_guardrail_evaluation_is_timed_separately_from_the_tool(self, settings, spec_path):
        ledger = RunLedger()
        with use_ledger(ledger):
            registry(settings).call("fetch_document", url=str(spec_path))

        guardrail = ledger.of_kind("guardrail")
        tool = ledger.of_kind("tool")
        assert len(guardrail) == 1 and len(tool) == 1
        assert guardrail[0].name == "guardrail:fetch_document"

    def test_a_blocked_call_still_records_its_guardrail_cost(self, settings):
        ledger = RunLedger()
        with use_ledger(ledger):
            registry(settings).call("purge_cache", pattern="*")
        assert len(ledger.of_kind("guardrail")) == 1
        assert len(ledger.of_kind("tool")) == 0, "a blocked call must not be timed as executed"


class TestDisabledGuardrails:
    def test_disabling_guardrails_lets_a_forbidden_call_through(self, settings):
        """The benchmark control arm — and a demonstration of what the policy buys."""
        open_settings = settings.replace(guardrails_enabled=False)
        executed = []
        reg = ToolRegistry(settings=open_settings, approver=deny_all_approver)
        reg.tools["purge_cache"] = lambda **kw: executed.append(kw) or {"ok": True}

        result = reg.call("purge_cache", pattern="*")

        assert result.ok and len(executed) == 1
        assert result.decision.action is Action.ALLOW
