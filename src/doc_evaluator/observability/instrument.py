"""Timing helpers and the ambient ledger for the current run.

The ledger is held in a ``ContextVar`` rather than threaded through every
signature: LangGraph node functions receive only ``(state, config)``, so an
explicit parameter would mean smuggling the ledger through the state dict and
serialising it on every hop.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

from .ledger import RunLedger, Span, SpanKind

_current_ledger: ContextVar[RunLedger | None] = ContextVar("doc_eval_ledger", default=None)

F = TypeVar("F", bound=Callable[..., Any])


def get_ledger() -> RunLedger | None:
    return _current_ledger.get()


@contextmanager
def use_ledger(ledger: RunLedger) -> Iterator[RunLedger]:
    token = _current_ledger.set(ledger)
    try:
        yield ledger
    finally:
        _current_ledger.reset(token)


@contextmanager
def timed(name: str, kind: SpanKind, **detail: Any) -> Iterator[Span]:
    """Time a block and record it against the ambient ledger, if one is set."""
    span = Span(name=name, kind=kind, wall_ms=0.0, detail=detail)
    start = time.perf_counter()
    try:
        yield span
    finally:
        span.wall_ms = (time.perf_counter() - start) * 1000
        ledger = get_ledger()
        if ledger is not None:
            ledger.record(span)


def instrument(name: str, kind: SpanKind = "node") -> Callable[[F], F]:
    """Decorator form of :func:`timed` for node and tool functions."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with timed(name, kind):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def emit_to_agentops(ledger: RunLedger, api_key: str | None) -> bool:
    """Best-effort mirror of the ledger into AgentOps.

    Optional by construction: the project never requires an AgentOps account,
    and a missing package or a failed export must not affect the run.
    """
    if not api_key:
        return False
    try:
        import agentops  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        agentops.init(api_key=api_key, auto_start_session=False)
        session = agentops.start_session(tags=["doc-evaluator", ledger.run_id])
        for span in ledger.spans:
            agentops.record(
                agentops.ActionEvent(
                    action_type=f"{span.kind}:{span.name}",
                    returns={"wall_ms": span.wall_ms, "usd": span.usd},
                )
            )
        agentops.end_session("Success", session=session)
        return True
    except Exception:
        return False
