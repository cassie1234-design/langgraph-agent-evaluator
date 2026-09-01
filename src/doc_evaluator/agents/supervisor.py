"""The supervisor: decides which specialist runs next.

The routing prompt and the conditions below are the core of this project, so
the design is worth stating plainly.

**The model proposes; deterministic code disposes.** A supervisor that acts
directly on model output has two failure modes that show up immediately in
practice: it re-dispatches a worker that already succeeded (burning budget in a
loop), and it dispatches a worker whose inputs do not exist yet (the validator
with nothing to validate). Both are cheap to check in code and expensive to fix
with prompting. So the model picks among moves, and
:func:`_apply_preconditions` decides whether that move is legal.

That inversion is also what makes routing auditable: every hop lands in
``route_log`` tagged with whether the model, a precondition, or a budget
ceiling produced it.
"""

from __future__ import annotations

from typing import Any, Literal

from langgraph.types import Command

from ..config import Settings
from ..guardrails.budget import BudgetGuard
from ..llm import ModelCallError, render_context, structured_call
from ..observability.instrument import get_ledger, timed
from ..schemas import RouteDecision
from ..state import EvalState, RouteRecord

SUPERVISOR_SYSTEM = """\
You coordinate a three-specialist team that evaluates the quality of an API \
specification. You do not evaluate anything yourself and you do not call tools. \
Your only job is to choose which specialist runs next.

The specialists, and what each one needs before it can run:

  fetcher    Retrieves the target document and parses it into a spec object.
             Needs: nothing. Produces: the parsed specification.
  validator  Runs the deterministic rule set over the parsed spec, then adds
             qualitative judgements about documentation quality.
             Needs: a parsed specification. Produces: findings.
  reporter   Turns findings into a scored evaluation report.
             Needs: validation to have run. Produces: the final report.

Rules you must follow:
  1. Never choose a specialist whose inputs do not exist yet.
  2. Never re-run a specialist that already succeeded unless new information
     has arrived that would change its output.
  3. Choose "finish" as soon as a report exists. A finished evaluation is worth
     more than a marginally better one that costs another round trip.
  4. Zero findings is a valid, successful result — a clean specification is not
     evidence that the validator failed.

Answer with the single next step and a one-sentence justification."""

SUPERVISOR_PROMPT = """\
Evaluate the current state of this run and choose the next step.

Target under evaluation: {target}

{context}

Which specialist should run next, or is the evaluation finished?"""


def _facts(state: EvalState) -> dict[str, Any]:
    """The machine-readable state the router reasons over.

    Digested rather than raw: handing the model the whole specification on every
    hop would cost more per routing decision than the actual evaluation.
    """
    artifacts = state.get("artifacts") or {}
    findings = state.get("findings") or []
    return {
        "completed": list(state.get("completed") or []),
        "has_artifact": bool(artifacts.get("spec")),
        "artifact_source": artifacts.get("source"),
        "operation_count": artifacts.get("operation_count", 0),
        "finding_count": len(findings),
        "has_report": bool(state.get("report")),
        "iteration": state.get("iteration", 0),
        "last_error": artifacts.get("last_error"),
        # A fetch that already ran and produced nothing is the difference between
        # "not fetched yet" and "cannot be fetched". Without these two facts the
        # ladder below cannot tell them apart, and re-dispatches forever.
        "fetch_attempts": list(state.get("completed") or []).count("fetcher"),
        "fetch_refused": bool(artifacts.get("refused")),
    }


# One retry, not eleven. The tool layer already does its own exponential backoff
# for transient transport failures, so a second graph-level attempt covers the
# case where that ran out, and nothing beyond it is worth paying for.
MAX_FETCH_ATTEMPTS = 2


def _apply_preconditions(proposal: str, facts: dict[str, Any]) -> tuple[str, str | None]:
    """Return ``(route, override_reason)``. ``None`` means the model's choice stood."""
    completed = set(facts["completed"])

    # This comes first because every rule below assumes a missing specification
    # is still obtainable. When the fetcher has already run and come back
    # empty-handed, it is not: routing to it again produces an identical failure
    # and pays for another routing call to discover that. Left unchecked the run
    # burns every iteration in its budget before the ceiling stops it.
    if not facts["has_artifact"] and facts.get("fetch_attempts"):
        if facts.get("fetch_refused"):
            # A policy refusal is terminal by contract - the refusal payload says
            # so in as many words - so a retry cannot change the outcome.
            return "finish", (
                "The fetch was refused by policy, which is terminal by contract; "
                "retrying cannot change the outcome."
            )
        if facts["fetch_attempts"] >= MAX_FETCH_ATTEMPTS:
            detail = facts.get("last_error") or "no specification was returned"
            return "finish", (
                f"The fetch failed on {facts['fetch_attempts']} attempts ({detail}); "
                "there is nothing to validate."
            )
        return "fetcher", (
            "The fetch produced no specification; retrying once before giving up "
            f"(attempt {facts['fetch_attempts'] + 1} of {MAX_FETCH_ATTEMPTS})."
        )

    if proposal == "validator" and not facts["has_artifact"]:
        return "fetcher", "Validation needs a parsed specification; none has been fetched yet."

    if proposal == "reporter" and not facts["has_artifact"]:
        return "fetcher", "Reporting needs a specification; none has been fetched yet."

    if proposal == "reporter" and "validator" not in completed:
        return "validator", "Reporting needs validation results; validation has not run."

    if proposal == "fetcher" and facts["has_artifact"]:
        # Re-fetching an artifact we already hold is the classic supervisor loop.
        nxt = "validator" if "validator" not in completed else "reporter"
        return nxt, "The specification is already in hand; re-fetching would repeat work."

    if proposal == "validator" and "validator" in completed:
        return "reporter", "Validation already ran; the findings are ready to report."

    if proposal == "finish" and not facts["has_report"]:
        if not facts["has_artifact"]:
            return "fetcher", "Cannot finish before anything has been fetched."
        if "validator" not in completed:
            return "validator", "Cannot finish before the specification has been validated."
        return "reporter", "Cannot finish before a report has been written."

    return proposal, None


def make_supervisor(settings: Settings):
    """Build the supervisor node bound to a configuration."""
    budget = BudgetGuard(
        max_iterations=settings.max_iterations,
        max_total_tokens=settings.max_total_tokens,
        max_wall_seconds=settings.max_wall_seconds,
    )

    # The Literal return annotation is how LangGraph discovers the edges a
    # Command-routing node can take. Without it the compiled graph works but
    # renders as a supervisor wired to nothing, which makes the diagram in the
    # docs and the UI's topology view silently wrong.
    def supervisor(
        state: EvalState,
    ) -> Command[Literal["fetcher", "validator", "reporter", "__end__"]]:
        with timed("node:supervisor", "node"):
            iteration = state.get("iteration", 0)
            facts = _facts(state)

            # Ceilings come first: an over-budget run must not pay for one more
            # routing call just to be told to stop.
            status = budget.check(iteration, get_ledger())
            if status.exceeded:
                can_still_report = facts["has_artifact"] and not facts["has_report"]
                target = "reporter" if can_still_report else "__end__"
                return Command(
                    goto=target,
                    update={
                        "iteration": iteration + 1,
                        "halt_reason": status.reason,
                        "route_log": [
                            RouteRecord(
                                iteration=iteration,
                                decision=target,
                                reason=f"Budget ceiling: {status.reason}",
                                source="budget",
                            )
                        ],
                    },
                )

            if facts["has_report"]:
                return Command(
                    goto="__end__",
                    update={
                        "iteration": iteration + 1,
                        "route_log": [
                            RouteRecord(
                                iteration=iteration,
                                decision="finish",
                                reason="A report exists; the evaluation is complete.",
                                source="completion",
                            )
                        ],
                    },
                )

            prompt = SUPERVISOR_PROMPT.format(
                target=state.get("target", "(unspecified)"),
                context=render_context(facts),
            )

            try:
                proposal = structured_call(
                    "supervisor", RouteDecision, prompt, settings, system=SUPERVISOR_SYSTEM
                )
                choice, confidence = proposal.next, proposal.confidence
                reason = proposal.reason
                source = "model"
            except ModelCallError as exc:
                # The router failing is not fatal: the precondition ladder below
                # is a complete fallback policy on its own.
                choice, confidence, source = "finish", None, "precondition"
                reason = f"Router unavailable ({exc}); falling back to precondition ordering."

            final, override = _apply_preconditions(choice, facts)
            if override is not None:
                reason = f"{override} (router proposed {choice!r})"
                source = "precondition"

            goto = "__end__" if final == "finish" else final
            update: dict[str, Any] = {
                "iteration": iteration + 1,
                "next_worker": final,
            }
            # Finishing with nothing to show is an outcome the caller has to be
            # able to see; without this the CLI reports a bare zero score and no
            # explanation of why the evaluation never happened.
            if final == "finish" and not facts["has_report"]:
                update["halt_reason"] = reason

            return Command(
                goto=goto,
                update={
                    **update,
                    "route_log": [
                        RouteRecord(
                            iteration=iteration,
                            decision=final,
                            reason=reason,
                            source=source,  # type: ignore[arg-type]
                            confidence=confidence,
                        )
                    ],
                },
            )

    return supervisor
