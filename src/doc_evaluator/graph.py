"""Graph assembly and the run driver.

Topology is a star: ``supervisor -> worker -> supervisor``, with the supervisor
routing via ``Command(goto=...)``. Workers never route — a worker that decides
what runs next is a second supervisor, and two routers disagreeing is a class of
bug that is very hard to see in a trace.

The graph is compiled with a checkpointer because HIGH_RISK tool calls suspend
the run with ``interrupt()``. Without persisted state there is nothing to resume
into, so the checkpointer is a requirement of the guardrail design rather than
an optional extra.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import START, StateGraph
from langgraph.types import Command

from .agents import make_fetcher, make_reporter, make_supervisor, make_validator
from .config import Settings
from .guardrails.engine import GuardrailEngine
from .observability.instrument import use_ledger
from .observability.ledger import RunLedger
from .state import EvalState, Finding, GuardrailEvent, RouteRecord
from .tools.registry import Approver, ToolRegistry, interrupt_approver

WORKERS = ("fetcher", "validator", "reporter")

# Our own Pydantic models cross the checkpoint boundary on every interrupt.
# LangGraph refuses to deserialize unregistered types (and will hard-fail on
# them in a future release), so they are declared rather than left to a warning.
CHECKPOINT_TYPES = (Finding, GuardrailEvent, RouteRecord)


def make_checkpointer() -> MemorySaver:
    """An in-memory checkpointer that knows how to round-trip our state types.

    In-memory is the right scope here: a checkpoint exists to survive a
    human-approval interrupt within one evaluation, not to outlive the process.
    Swapping in a persistent saver is a one-line change if that ever matters.
    """
    # The allowlist must be passed to the constructor. ``with_msgpack_allowlist``
    # silently returns ``self`` when the base allowlist is the permissive default,
    # which is exactly the case here — it would look like it worked and change
    # nothing.
    return MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=CHECKPOINT_TYPES))


def build_graph(
    settings: Settings,
    approver: Approver = interrupt_approver,
    checkpointer: Any | None = None,
):
    """Compile the evaluation graph."""
    engine = GuardrailEngine(
        allowed_hosts=settings.allowed_hosts, enabled=settings.guardrails_enabled
    )
    registry = ToolRegistry(settings=settings, engine=engine, approver=approver)

    builder = StateGraph(EvalState)
    builder.add_node("supervisor", make_supervisor(settings))
    builder.add_node("fetcher", make_fetcher(settings, registry))
    builder.add_node("validator", make_validator(settings, registry))
    builder.add_node("reporter", make_reporter(settings, registry))

    builder.add_edge(START, "supervisor")
    for worker in WORKERS:
        builder.add_edge(worker, "supervisor")

    return builder.compile(checkpointer=checkpointer or make_checkpointer())


@dataclass
class RunResult:
    """Everything a caller needs to render or assert on a completed run."""

    state: dict[str, Any]
    ledger: RunLedger
    interrupted: bool = False
    interrupt_payload: dict[str, Any] | None = None
    thread_id: str = ""

    @property
    def report(self) -> str | None:
        return self.state.get("report")

    @property
    def score(self) -> dict[str, Any] | None:
        return self.state.get("score")

    @property
    def findings(self) -> list:
        return self.state.get("findings") or []

    def summary(self) -> dict[str, Any]:
        score = self.score or {}
        return {
            "thread_id": self.thread_id,
            "target": self.state.get("target"),
            "score": score.get("score"),
            "grade": score.get("grade"),
            "findings": len(self.findings),
            "interrupted": self.interrupted,
            "halt_reason": self.state.get("halt_reason"),
            "hops": len(self.state.get("route_log") or []),
            "guardrail_events": len(self.state.get("guardrail_log") or []),
            **self.ledger.summary(),
        }


@dataclass
class Evaluation:
    """A single evaluation run, resumable across human-approval interrupts."""

    settings: Settings
    approver: Approver = interrupt_approver
    thread_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    _graph: Any = field(default=None, repr=False)
    _ledger: RunLedger | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._graph = build_graph(self.settings, approver=self.approver)
        self._ledger = RunLedger(run_id=self.thread_id, model=self.settings.model)

    @property
    def config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}, "recursion_limit": 60}

    @property
    def ledger(self) -> RunLedger:
        assert self._ledger is not None
        return self._ledger

    def _initial(self, target: str) -> dict[str, Any]:
        return {
            "target": target,
            "artifacts": {},
            "findings": [],
            "completed": [],
            "route_log": [],
            "guardrail_log": [],
            "iteration": 0,
            "report": None,
            "score": None,
            "halt_reason": None,
        }

    def _finish(self) -> RunResult:
        snapshot = self._graph.get_state(self.config)
        interrupts = getattr(snapshot, "interrupts", ()) or ()
        payload = interrupts[0].value if interrupts else None
        return RunResult(
            state=dict(snapshot.values),
            ledger=self.ledger,
            interrupted=bool(interrupts),
            interrupt_payload=payload,
            thread_id=self.thread_id,
        )

    def start(self, target: str) -> RunResult:
        with use_ledger(self.ledger):
            self._graph.invoke(self._initial(target), self.config)
        return self._finish()

    def resume(self, approved: bool) -> RunResult:
        """Continue a run suspended at a HIGH_RISK approval."""
        with use_ledger(self.ledger):
            self._graph.invoke(Command(resume={"approved": approved}), self.config)
        return self._finish()

    def stream(self, target: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yield ``(node_name, update)`` as the graph advances, for live UIs."""
        with use_ledger(self.ledger):
            for chunk in self._graph.stream(self._initial(target), self.config):
                yield from chunk.items()


def evaluate(
    target: str,
    settings: Settings | None = None,
    approver: Approver = interrupt_approver,
) -> RunResult:
    """Run one evaluation to completion (or to its first approval interrupt)."""
    run = Evaluation(settings=settings or Settings.from_env(), approver=approver)
    return run.start(target)
