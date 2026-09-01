"""The guardrail sandbox: every advertised call reaches its advertised outcome."""

from __future__ import annotations

import pytest
from langgraph.types import Command

from doc_evaluator.demo import SANDBOX_CALLS, build_approval_demo_graph
from doc_evaluator.guardrails.engine import GuardrailEngine, Tier


def run(settings, tool, args, thread, resume=None):
    graph = build_approval_demo_graph(settings, tool, args)
    config = {"configurable": {"thread_id": thread}}
    graph.invoke({}, config)
    snapshot = graph.get_state(config)
    if snapshot.interrupts and resume is not None:
        graph.invoke(Command(resume={"approved": resume}), config)
        snapshot = graph.get_state(config)
    return graph, config, snapshot


@pytest.mark.parametrize("label", list(SANDBOX_CALLS))
def test_every_sandbox_call_reaches_a_terminal_outcome(settings, label):
    spec = SANDBOX_CALLS[label]
    engine = GuardrailEngine(allowed_hosts=settings.allowed_hosts)
    tier = engine.evaluate(spec["tool"], spec["args"]).tier

    _graph, _config, snapshot = run(
        settings, spec["tool"], spec["args"], f"t-{abs(hash(label))}", resume=False
    )
    assert not snapshot.interrupts, "the call is still suspended after an answer"
    assert snapshot.values["outcome"] in {"executed", "refused"}

    if tier is Tier.FORBIDDEN:
        assert snapshot.values["outcome"] == "refused"
    elif tier in (Tier.SAFE, Tier.SENSITIVE):
        assert snapshot.values["outcome"] == "executed"


@pytest.mark.parametrize("label", list(SANDBOX_CALLS))
def test_sandbox_labels_match_the_policy_they_claim(settings, label):
    """A label that says 'blocked' must describe a call the policy actually blocks."""
    spec = SANDBOX_CALLS[label]
    engine = GuardrailEngine(allowed_hosts=settings.allowed_hosts)
    decision = engine.evaluate(spec["tool"], spec["args"])

    lowered = label.lower()
    if "blocked" in lowered:
        assert decision.blocked, f"{label!r} claims to be blocked but is {decision.action.value}"
    elif "needs approval" in lowered:
        assert decision.needs_approval, f"{label!r} claims approval but is {decision.action.value}"
    elif "allowed" in lowered:
        assert decision.allowed and not decision.needs_approval


def test_forbidden_sandbox_call_never_pauses(settings):
    """A FORBIDDEN call must not be offered to a human as an approvable decision."""
    _graph, _config, snapshot = run(
        settings, "purge_cache", {"pattern": "*"}, "forbidden-never-asks"
    )
    assert not snapshot.interrupts
    assert snapshot.values["outcome"] == "refused"


def test_sandbox_does_not_execute_the_real_implementation(settings):
    _graph, _config, snapshot = run(
        settings, "purge_cache", {"pattern": "specs/x.json"}, "stubbed", resume=True
    )
    assert snapshot.values["detail"]["value"]["simulated"] is True
