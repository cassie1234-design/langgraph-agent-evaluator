"""Runtime configuration.

Everything the system needs to decide *how* to run — which model, which mode,
and where the guardrail ceilings sit — is resolved here once and passed around
explicitly. Nothing else in the package reads ``os.environ``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Mode = Literal["auto", "mock", "live"]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
CASSETTE_DIR = FIXTURES_DIR / "llm_cassettes"

DEFAULT_ALLOWED_HOSTS = (
    "raw.githubusercontent.com",
    "petstore3.swagger.io",
    "api.github.com",
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the process configuration."""

    mode: Mode = "auto"
    model: str = "claude-opus-5"
    api_key: str | None = None
    max_tokens: int = 8000

    # Guardrail ceilings. These are the "boring" protections every agent loop
    # needs; the interesting policy work lives in guardrails/policy.yaml.
    max_iterations: int = 12
    max_total_tokens: int = 200_000
    max_wall_seconds: float = 300.0

    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    guardrails_enabled: bool = True
    agentops_api_key: str | None = None

    # Retry/backoff for transient tool and API failures.
    max_retries: int = 3
    backoff_base_seconds: float = 0.5

    cassette_dir: Path = field(default=CASSETTE_DIR)

    @classmethod
    def from_env(cls, **overrides: object) -> Settings:
        raw_mode = os.environ.get("DOC_EVAL_MODE", "auto").strip().lower()
        mode: Mode = raw_mode if raw_mode in ("auto", "mock", "live") else "auto"
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip() or None

        base = cls(
            mode=mode,
            model=os.environ.get("DOC_EVAL_MODEL", "claude-opus-5").strip() or "claude-opus-5",
            api_key=key,
            max_iterations=_env_int("DOC_EVAL_MAX_ITERATIONS", 12),
            max_total_tokens=_env_int("DOC_EVAL_MAX_TOKENS", 200_000),
            max_wall_seconds=float(_env_int("DOC_EVAL_MAX_WALL_SECONDS", 300)),
            allowed_hosts=_env_csv("DOC_EVAL_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS),
            agentops_api_key=os.environ.get("AGENTOPS_API_KEY", "").strip() or None,
        )
        return base.replace(**overrides) if overrides else base

    def replace(self, **overrides: object) -> Settings:
        from dataclasses import replace as _replace

        return _replace(self, **overrides)  # type: ignore[arg-type]

    @property
    def resolved_mode(self) -> Literal["mock", "live"]:
        """``auto`` collapses to live only when we actually hold a key."""
        if self.mode == "live":
            return "live"
        if self.mode == "mock":
            return "mock"
        return "live" if self.api_key else "mock"

    @property
    def is_mock(self) -> bool:
        return self.resolved_mode == "mock"
