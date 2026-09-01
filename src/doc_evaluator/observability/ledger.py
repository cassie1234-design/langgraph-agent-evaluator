"""Per-run accounting of latency, tokens and cost.

One ``RunLedger`` per graph invocation. Every agent node, every guardrail
evaluation and every tool execution contributes a ``Span``. Because guardrail
evaluation gets its own span kind, "what do the guardrails cost?" is a query
against the ledger rather than a guess.
"""

from __future__ import annotations

import json
import statistics
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .pricing import cost_usd

SpanKind = Literal["node", "llm", "tool", "guardrail"]


@dataclass
class Span:
    name: str
    kind: SpanKind
    wall_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    usd: float = 0.0
    model: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class RunLedger:
    """Thread-safe collector of spans for a single run."""

    def __init__(self, run_id: str | None = None, model: str = "claude-opus-5") -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.model = model
        self.spans: list[Span] = []
        self.started_at = time.perf_counter()
        self._lock = threading.Lock()

    # -- recording ---------------------------------------------------------

    def record(self, span: Span) -> Span:
        if span.usd == 0.0 and span.total_tokens:
            span.usd = cost_usd(
                span.model or self.model,
                span.input_tokens,
                span.output_tokens,
                span.cache_read_tokens,
            )
        with self._lock:
            self.spans.append(span)
        return span

    def record_llm(
        self,
        name: str,
        wall_ms: float,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        model: str | None = None,
    ) -> Span:
        return self.record(
            Span(
                name=name,
                kind="llm",
                wall_ms=wall_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                model=model or self.model,
            )
        )

    # -- queries -----------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        return time.perf_counter() - self.started_at

    def of_kind(self, kind: SpanKind) -> list[Span]:
        return [s for s in self.spans if s.kind == kind]

    @property
    def total_tokens(self) -> int:
        return sum(s.total_tokens for s in self.spans)

    @property
    def total_usd(self) -> float:
        return sum(s.usd for s in self.spans)

    @property
    def guardrail_ms(self) -> float:
        """Wall time spent deciding whether calls were allowed."""
        return sum(s.wall_ms for s in self.of_kind("guardrail"))

    def by_name(self) -> dict[str, dict[str, float]]:
        grouped: dict[str, dict[str, float]] = {}
        for span in self.spans:
            row = grouped.setdefault(
                span.name, {"calls": 0, "wall_ms": 0.0, "tokens": 0, "usd": 0.0}
            )
            row["calls"] += 1
            row["wall_ms"] += span.wall_ms
            row["tokens"] += span.total_tokens
            row["usd"] += span.usd
        return grouped

    def summary(self) -> dict[str, Any]:
        guardrail_spans = self.of_kind("guardrail")
        evals = [s.wall_ms for s in guardrail_spans]
        return {
            "run_id": self.run_id,
            "model": self.model,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "spans": len(self.spans),
            "llm_calls": len(self.of_kind("llm")),
            "tool_calls": len(self.of_kind("tool")),
            "input_tokens": sum(s.input_tokens for s in self.spans),
            "output_tokens": sum(s.output_tokens for s in self.spans),
            "total_tokens": self.total_tokens,
            "total_usd": round(self.total_usd, 6),
            "guardrail_evaluations": len(guardrail_spans),
            "guardrail_total_ms": round(self.guardrail_ms, 4),
            "guardrail_mean_ms": round(statistics.fmean(evals), 5) if evals else 0.0,
            "by_name": self.by_name(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {"summary": self.summary(), "spans": [asdict(s) for s in self.spans]},
            indent=indent,
            default=str,
        )
