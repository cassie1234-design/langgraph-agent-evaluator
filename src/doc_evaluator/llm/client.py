"""One entry point for every model call in the system.

Callers ask for a structured result and get a validated Pydantic object. They
never see which mode is active, never touch ``ChatAnthropic`` directly, and
never parse model text. Usage lands in the run ledger either way, so the cost
panel and the benchmark are meaningful with or without an API key.
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings
from ..observability.instrument import get_ledger
from .replay import ReplayModel, estimate_tokens

T = TypeVar("T", bound=BaseModel)

_LIVE_CACHE: dict[tuple[str, int, str], Any] = {}


class ModelCallError(RuntimeError):
    """A model call failed after exhausting retries, or returned unusable output."""


def _live_model(settings: Settings):
    """Build (and memoise) a ChatAnthropic client.

    Note what is *not* set: ``temperature`` and ``thinking``. Claude Opus 5
    rejects sampling parameters outright, and runs adaptive thinking when the
    ``thinking`` parameter is omitted — so the correct configuration here is the
    empty one. Setting either would turn every call into a 400.
    """
    key = (settings.model, settings.max_tokens, settings.api_key or "")
    if key not in _LIVE_CACHE:
        from langchain_anthropic import ChatAnthropic

        _LIVE_CACHE[key] = ChatAnthropic(
            model=settings.model,
            max_tokens=settings.max_tokens,
            api_key=settings.api_key,
            max_retries=settings.max_retries,
            timeout=120.0,
        )
    return _LIVE_CACHE[key]


def _record(node: str, wall_ms: float, in_tok: int, out_tok: int, settings: Settings) -> None:
    ledger = get_ledger()
    if ledger is not None:
        ledger.record_llm(
            name=f"llm:{node}",
            wall_ms=wall_ms,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=settings.model,
        )


def _usage_from(raw: Any) -> tuple[int, int]:
    usage = getattr(raw, "usage_metadata", None) or {}
    return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


def structured_call(
    node: str,
    schema: type[T],
    prompt: str,
    settings: Settings,
    system: str | None = None,
) -> T:
    """Ask the model for a ``schema``-shaped answer. Raises :class:`ModelCallError`."""
    start = time.perf_counter()

    if settings.is_mock:
        replay = ReplayModel(settings.cassette_dir, settings.model)
        try:
            parsed, in_tok, out_tok = replay.structured(node, schema, prompt)
        except (LookupError, ValidationError) as exc:
            raise ModelCallError(f"replay failed for node {node!r}: {exc}") from exc
        _record(node, (time.perf_counter() - start) * 1000, in_tok, out_tok, settings)
        return parsed

    messages: list[tuple[str, str]] = []
    if system:
        messages.append(("system", system))
    messages.append(("human", prompt))

    model = _live_model(settings).with_structured_output(schema, include_raw=True)

    last_error: Exception | None = None
    for attempt in range(settings.max_retries):
        try:
            result = model.invoke(messages)
        except Exception as exc:  # network / rate-limit / transient API failures
            last_error = exc
            if attempt == settings.max_retries - 1:
                break
            time.sleep(settings.backoff_base_seconds * (2**attempt))
            continue

        raw = result.get("raw") if isinstance(result, dict) else None
        parsed = result.get("parsed") if isinstance(result, dict) else result
        in_tok, out_tok = _usage_from(raw)
        if not in_tok:
            in_tok = estimate_tokens(prompt + (system or ""))
        _record(node, (time.perf_counter() - start) * 1000, in_tok, out_tok, settings)

        if parsed is not None:
            return parsed  # type: ignore[return-value]

        # Schema rejected the response. Retrying is worthwhile — this is the
        # one failure the model can actually correct on a second attempt.
        last_error = (result or {}).get("parsing_error") or ModelCallError("empty parse")
        if attempt == settings.max_retries - 1:
            break
        time.sleep(settings.backoff_base_seconds * (2**attempt))

    raise ModelCallError(
        f"node {node!r} could not produce a valid {schema.__name__} "
        f"after {settings.max_retries} attempts: {last_error}"
    )
