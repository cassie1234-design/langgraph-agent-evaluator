"""Tool implementations and the guarded registry that fronts them."""

from .doc_tools import ToolError, fetch_document, parse_openapi, validate_spec
from .registry import (
    TOOLS,
    Approver,
    ToolRegistry,
    ToolResult,
    deny_all_approver,
    interrupt_approver,
)
from .risky_tools import purge_cache, send_external_report

__all__ = [
    "TOOLS",
    "Approver",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "deny_all_approver",
    "fetch_document",
    "interrupt_approver",
    "parse_openapi",
    "purge_cache",
    "send_external_report",
    "validate_spec",
]
