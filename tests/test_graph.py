"""End-to-end runs, and the human-in-the-loop interrupt cycle."""

from __future__ import annotations

from doc_evaluator.config import REPO_ROOT
from doc_evaluator.graph import Evaluation, build_graph, evaluate
from doc_evaluator.samples import SYNTHETIC_CREDENTIAL
from doc_evaluator.tools.registry import interrupt_approver

SPECS = REPO_ROOT / "fixtures" / "specs"


class TestFullRun:
    def test_clean_spec_runs_to_a_perfect_report(self, settings):
        result = evaluate(str(SPECS / "petstore.json"), settings)
        assert not result.interrupted
        assert result.score["score"] == 100
        assert result.report and "Petstore API" in result.report

    def test_flawed_spec_produces_findings_and_a_report(self, settings):
        result = evaluate(str(SPECS / "legacy_billing.yaml"), settings)
        assert len(result.findings) > 10
        assert result.score["grade"] == "F"
        assert "Remediation priorities" in result.report

    def test_workers_run_exactly_once_each(self, settings):
        result = evaluate(str(SPECS / "petstore.json"), settings)
        completed = result.state["completed"]
        assert sorted(completed) == ["fetcher", "reporter", "validator"]
        assert len(completed) == len(set(completed)), "a worker ran twice"

    def test_routing_is_recorded_with_its_provenance(self, settings):
        result = evaluate(str(SPECS / "petstore.json"), settings)
        log = result.state["route_log"]
        assert [r.decision for r in log][:3] == ["fetcher", "validator", "reporter"]
        assert all(r.source in {"model", "precondition", "budget", "completion"} for r in log)
        assert all(r.reason for r in log)

    def test_every_tool_call_is_audited(self, settings):
        result = evaluate(str(SPECS / "petstore.json"), settings)
        tools = {e.tool for e in result.state["guardrail_log"]}
        assert tools == {"fetch_document", "parse_openapi", "validate_spec"}

    def test_run_is_priced(self, settings):
        result = evaluate(str(SPECS / "petstore.json"), settings)
        summary = result.summary()
        assert summary["total_usd"] > 0 and summary["llm_calls"] >= 3
        assert summary["guardrail_evaluations"] == 3

    def test_results_are_reproducible(self, settings):
        a = evaluate(str(SPECS / "legacy_billing.yaml"), settings)
        b = evaluate(str(SPECS / "legacy_billing.yaml"), settings)
        assert a.score == b.score
        assert [f.rule_id for f in a.findings] == [f.rule_id for f in b.findings]

    def test_structurally_broken_spec_still_produces_a_report(self, settings):
        result = evaluate(str(SPECS / "broken_inventory.json"), settings)
        assert result.report and result.score["severity_counts"]["critical"] > 0


class TestFailureHandling:
    def test_missing_target_degrades_without_crashing(self, settings):
        result = evaluate(str(SPECS / "does_not_exist.json"), settings)
        assert result.state["artifacts"]["last_error"]
        assert "fetcher" in result.state["completed"]

    def test_ssrf_target_is_refused_and_the_run_still_terminates(self, settings):
        result = evaluate("http://169.254.169.254/latest/meta-data/", settings)
        artifacts = result.state["artifacts"]
        assert artifacts["refused"] is True
        assert artifacts["refusal"]["risk_tier"] == "FORBIDDEN"
        assert not result.state.get("spec")

    def test_iteration_ceiling_halts_the_run(self, settings):
        capped = settings.replace(max_iterations=1)
        result = evaluate(str(SPECS / "petstore.json"), capped)
        assert result.state["halt_reason"]
        assert "iteration ceiling" in result.state["halt_reason"]


class TestRedactionInPipeline:
    def test_credentials_are_stripped_before_reaching_the_model(self, settings):
        result = evaluate(str(SPECS / "legacy_billing.yaml"), settings)
        labels = result.state["artifacts"]["redacted"]
        assert "anthropic_key" in labels and "email" in labels

    def test_the_report_records_that_redaction_happened(self, settings):
        result = evaluate(str(SPECS / "legacy_billing.yaml"), settings)
        assert "Redacted before analysis" in result.report

    def test_the_secret_never_appears_in_the_report(self, settings):
        result = evaluate(str(SPECS / "legacy_billing.yaml"), settings)
        assert SYNTHETIC_CREDENTIAL not in result.report


class TestHumanInTheLoop:
    """The HIGH_RISK path: interrupt, resume, and both answers.

    Driven through the registry with the real ``interrupt_approver`` inside a
    graph node, so this exercises the genuine LangGraph suspend/resume cycle
    rather than a stubbed approval.
    """

    @staticmethod
    def _risky_graph(settings, approver):
        """A one-node graph whose node makes a HIGH_RISK call."""
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, StateGraph
        from typing_extensions import TypedDict

        from doc_evaluator.tools.registry import ToolRegistry

        class S(TypedDict, total=False):
            outcome: str
            approved: bool

        registry = ToolRegistry(settings=settings, approver=approver)
        registry.tools["purge_cache"] = lambda **kw: {"deleted": ["x"], "count": 1}

        def node(state):
            result = registry.call("purge_cache", pattern="specs/x.json")
            return {"outcome": "ran" if result.ok else "refused", "approved": result.ok}

        builder = StateGraph(S)
        builder.add_node("risky", node)
        builder.add_edge(START, "risky")
        builder.add_edge("risky", END)
        return builder.compile(checkpointer=MemorySaver())

    def test_high_risk_call_suspends_the_graph(self, settings):
        graph = self._risky_graph(settings, interrupt_approver)
        config = {"configurable": {"thread_id": "t1"}}
        graph.invoke({}, config)

        snapshot = graph.get_state(config)
        assert snapshot.interrupts, "the graph did not pause for approval"
        payload = snapshot.interrupts[0].value
        assert payload["kind"] == "guardrail_approval"
        assert payload["risk_tier"] == "HIGH_RISK"
        assert payload["tool"] == "purge_cache"
        assert "pattern" in payload["arguments"]

    def test_resuming_with_approval_executes_the_call(self, settings):
        from langgraph.types import Command

        graph = self._risky_graph(settings, interrupt_approver)
        config = {"configurable": {"thread_id": "t2"}}
        graph.invoke({}, config)
        graph.invoke(Command(resume={"approved": True}), config)

        assert graph.get_state(config).values["outcome"] == "ran"

    def test_resuming_with_denial_refuses_the_call(self, settings):
        from langgraph.types import Command

        graph = self._risky_graph(settings, interrupt_approver)
        config = {"configurable": {"thread_id": "t3"}}
        graph.invoke({}, config)
        graph.invoke(Command(resume={"approved": False}), config)

        assert graph.get_state(config).values["outcome"] == "refused"

    def test_the_evaluation_path_never_needs_approval(self, settings):
        """A documentation evaluator has no business calling a HIGH_RISK tool."""
        run = Evaluation(settings=settings, approver=interrupt_approver)
        result = run.start(str(SPECS / "petstore.json"))
        assert not result.interrupted
        assert all(e.action != "CONFIRM" for e in result.state["guardrail_log"])


class TestGraphShape:
    def test_graph_compiles(self, settings):
        assert build_graph(settings) is not None

    def test_workers_route_back_to_the_supervisor_only(self, settings):
        """Only the supervisor routes. A worker that picks the next node is a
        second router, and two routers disagreeing is very hard to see in a trace."""
        edges = build_graph(settings).get_graph().edges
        for worker in ("fetcher", "validator", "reporter"):
            targets = {e.target for e in edges if e.source == worker}
            assert targets == {"supervisor"}, f"{worker} routes somewhere other than supervisor"

    def test_supervisor_can_reach_every_worker_and_the_end(self, settings):
        """Guards the Command return annotation: without it the compiled graph
        still runs, but renders as a supervisor wired to nothing."""
        edges = build_graph(settings).get_graph().edges
        targets = {e.target for e in edges if e.source == "supervisor"}
        assert targets == {"fetcher", "validator", "reporter", "__end__"}

    def test_streaming_yields_each_node(self, settings):
        run = Evaluation(settings=settings, approver=lambda d, a: False)
        nodes = [node for node, _update in run.stream(str(SPECS / "petstore.json"))]
        assert "fetcher" in nodes and "validator" in nodes and "reporter" in nodes
