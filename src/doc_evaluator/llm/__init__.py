"""Model access: one structured entry point, two interchangeable backends."""

from .client import ModelCallError, structured_call
from .prompt_context import extract as extract_context
from .prompt_context import render as render_context
from .replay import ReplayModel, estimate_tokens

__all__ = [
    "ModelCallError",
    "ReplayModel",
    "estimate_tokens",
    "extract_context",
    "render_context",
    "structured_call",
]
