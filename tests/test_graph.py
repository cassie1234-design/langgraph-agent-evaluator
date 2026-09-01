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

    def test_a_refused_fetch_stops_after_one_attempt(self, settings):
        """Regression: this used to retry until the iteration ceiling.

        Twelve fetches, twelve routing calls and twelve times the intended spend,
        all to rediscover a refusal that is terminal by contract.
        """
        result = evaluate("http://169.254.169.254/latest/meta-data/", settings)
        assert result.state["completed"].count("fetcher") == 1
        assert len(result.state["route_log"]) <= 3
        assert "terminal" in (result.state.get("halt_reason") or "")

    def test_an_unreachable_target_gets_exactly_one_retry(self, settings):
        result = evaluate(str(SPECS / "does_not_exist.json"), settings)
        assert result.state["completed"].count("fetcher") == 2
        assert "there is nothing to validate" in (result.state.get("halt_reason") or "")

    def test_a_doomed_run_costs_a_fraction_of_a_real_one(self, settings):
        """The point of stopping early is not tidiness, it is spend."""
        doomed = evaluate("http://169.254.169.254/latest/meta-data/", settings)
        real = evaluate(str(SPECS / "petstore.json"), settings)
        assert doomed.ledger.total_usd < real.ledger.total_usd

    def test_iteration_ceiling_halts_the_run(self, settings):
        capped = settings.replace(max_iterations=1)
        result = evaluate(str(SPECS / "petstore.json"), capped)
        assert result.state["halt_reason"]
        assert "iteration ceiling" in result.state["halt_reason"]


class TestFindingDeduplication:
    """The deterministic and qualitative passes must not both report one defect."""

    def test_no_two_findings_share_a_location_and_category(self, settings):
        for fixture in ("petstore.json", "legacy_billing.yaml", "broken_inventory.json"):
            result = evaluate(str(SPECS / fixture), settings)
            pairs = [(f.json_path, f.category) for f in result.findings]
            assert len(pairs) == len(set(pairs)), f"{fixture} double-counts a defect"

    def test_qualitative_finding_is_suppressed_when_a_rule_covers_it(self, settings):
        """`llm.no_prose` and `op.no_documentation` are the same observation.

        They carry different rule ids, so id-based deduplication cannot catch
        the overlap — only matching on (json_path, category) can.
        """
        result = evaluate(str(SPECS / "broken_inventory.json"), settings)
        clarity = [f for f in result.findings if f.category == "clarity"]
        by_path = {}
        for finding in clarity:
            by_path.setdefault(finding.json_path, []).append(finding)
        assert all(len(group) == 1 for group in by_path.values())

    def test_qualitative_findings_survive_where_they_are_genuinely_new(self, settings):
        """Deduplication must not silently delete the model's actual contribution."""
        result = evaluate(str(SPECS / "legacy_billing.yaml"), settings)
        llm_findings = [f for f in result.findings if f.source == "llm"]
        assert llm_findings, "the qualitative pass contributed nothing"
        assert {f.rule_id for f in llm_findings} <= {
            "llm.thin_prose",
            "llm.no_prose",
            "llm.auth_unexplained",
        }


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
        """The shipped sandbox graph, so the tests exercise what the UI runs."""
        from doc_evaluator.demo import build_approval_demo_graph

        return build_approval_demo_graph(
            settings, "purge_cache", {"pattern": "specs/x.json"}, approver
        )

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

        values = graph.get_state(config).values
        assert values["outcome"] == "executed"
        assert values["detail"]["approved"] is True

    def test_resuming_with_denial_refuses_the_call(self, settings):
        from langgraph.types import Command

        graph = self._risky_graph(settings, interrupt_approver)
        config = {"configurable": {"thread_id": "t3"}}
        graph.invoke({}, config)
        graph.invoke(Command(resume={"approved": False}), config)

        values = graph.get_state(config).values
        assert values["outcome"] == "refused"
        assert values["detail"]["value"]["status"] == "denied_by_operator"

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
