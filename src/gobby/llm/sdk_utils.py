"""Shared utilities for Claude Agent SDK integration.

Functions extracted from claude_streaming.py, chat_session.py, and
chat_session_helpers.py to eliminate duplication across SDK consumers.
"""

from __future__ import annotations


def sanitize_error(e: Exception) -> str:
    """Return a user-facing error message, hiding internal library details."""
    msg = str(e)
    if "litellm" in msg.lower() or "model isn't mapped" in msg or "custom_llm_provider" in msg:
        return "An internal error occurred. Please try again."
    return msg


def parse_server_name(full_tool_name: str) -> str:
    """Extract server name from mcp__{server}__{tool} format."""
    if full_tool_name.startswith("mcp__"):
        parts = full_tool_name.split("__")
        if len(parts) >= 2:
            return parts[1]
    return "builtin"


def format_exception_group(eg: ExceptionGroup) -> str:
    """Format an ExceptionGroup into a semicolon-separated error string."""
    errors = [sanitize_error(exc) for exc in eg.exceptions]
    return "; ".join(errors)


# Claude Code / Agent SDK hard-truncates additionalContext at 10K chars.
# We cap slightly below to avoid the ugly "... [output truncated]" suffix.
ADDITIONAL_CONTEXT_LIMIT = 9_950


def truncate_additional_context(text: str) -> str:
    """Truncate text to fit within the SDK's additionalContext limit."""
    if len(text) <= ADDITIONAL_CONTEXT_LIMIT:
        return text
    return text[: ADDITIONAL_CONTEXT_LIMIT - 16] + "\n... [truncated]"
