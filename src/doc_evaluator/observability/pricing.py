"""Token pricing, in USD per million tokens.

Kept as a plain table rather than an API lookup so that a run in mock mode
produces the same dollar figures as a live run — the benchmark compares cost
deltas, and those must not depend on network access.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelRate:
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    # Cache reads bill at 10% of the input rate.
    cache_read_multiplier: float = 0.10


PRICING: dict[str, ModelRate] = {
    "claude-opus-5": ModelRate(5.00, 25.00),
    "claude-opus-4-8": ModelRate(5.00, 25.00),
    "claude-sonnet-5": ModelRate(2.00, 10.00),
    "claude-haiku-4-5": ModelRate(1.00, 5.00),
}

# Unknown ids are billed at the Opus tier so cost is over- rather than
# under-reported. Silently reporting $0 for an unrecognised model would make
# the cost panel actively misleading.
FALLBACK_RATE = PRICING["claude-opus-5"]


def rate_for(model: str) -> ModelRate:
    return PRICING.get(model, FALLBACK_RATE)


def cost_usd(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    rate = rate_for(model)
    billable_input = max(input_tokens - cache_read_tokens, 0)
    return (
        billable_input * rate.input_usd_per_mtok
        + cache_read_tokens * rate.input_usd_per_mtok * rate.cache_read_multiplier
        + output_tokens * rate.output_usd_per_mtok
    ) / 1_000_000
