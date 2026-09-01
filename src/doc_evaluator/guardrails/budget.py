"""Loop and spend ceilings.

These are the unglamorous guardrails every agent loop needs: a supervisor that
keeps re-dispatching the same worker, or a validator that keeps finding one
more thing to check, will otherwise burn the budget without ever terminating.

The design choice worth defending: a tripped ceiling **degrades**, it does not
raise. The graph finishes the hop it is on and routes to the reporter with
whatever findings it has, so the operator gets a partial report plus an
explicit ``halt_reason``. A ``RuntimeError`` at token 200,001 would throw away
the work already paid for.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..observability.ledger import RunLedger


@dataclass(frozen=True)
class BudgetStatus:
    exceeded: bool
    reason: str | None = None
    limit: str | None = None

    @classmethod
    def ok(cls) -> BudgetStatus:
        return cls(exceeded=False)


@dataclass(frozen=True)
class BudgetGuard:
    max_iterations: int = 12
    max_total_tokens: int = 200_000
    max_wall_seconds: float = 300.0

    def check(self, iteration: int, ledger: RunLedger | None = None) -> BudgetStatus:
        if iteration >= self.max_iterations:
            return BudgetStatus(
                True,
                f"iteration ceiling reached ({iteration}/{self.max_iterations}); "
                "the supervisor is not converging",
                "max_iterations",
            )
        if ledger is not None:
            if ledger.total_tokens >= self.max_total_tokens:
                return BudgetStatus(
                    True,
                    f"token ceiling reached ({ledger.total_tokens:,}/"
                    f"{self.max_total_tokens:,} tokens, ${ledger.total_usd:.4f} spent)",
                    "max_total_tokens",
                )
            if ledger.elapsed_seconds >= self.max_wall_seconds:
                return BudgetStatus(
                    True,
                    f"wall-clock ceiling reached ({ledger.elapsed_seconds:.1f}s/"
                    f"{self.max_wall_seconds:.0f}s)",
                    "max_wall_seconds",
                )
        return BudgetStatus.ok()
