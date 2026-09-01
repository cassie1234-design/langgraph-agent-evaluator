"""Multi-agent API documentation evaluator.

A supervisor routes work to three specialists — fetcher, validator, reporter —
with every tool call passing through a tiered guardrail policy and every model
call priced into a per-run ledger.
"""

from .config import Settings
from .graph import Evaluation, RunResult, build_graph, evaluate
from .state import EvalState, Finding, GuardrailEvent, RouteRecord

__version__ = "0.1.0"

__all__ = [
    "EvalState",
    "Evaluation",
    "Finding",
    "GuardrailEvent",
    "RouteRecord",
    "RunResult",
    "Settings",
    "build_graph",
    "evaluate",
]
