"""Sanitizers for untrusted code-index summary inputs and outputs."""

from __future__ import annotations

SUMMARY_MAX_CHARS = 500


def sanitize_source_for_summary_prompt(source: str, *, max_chars: int) -> str:
    """Prepare indexed source for a fenced data-only prompt block."""
    return source[:max_chars].replace("`", "'").replace("\x00", "")


def sanitize_symbol_summary(summary: str) -> str | None:
    """Normalize generated summaries before storing them in code_symbols.summary."""
    sanitized = summary.split("```", 1)[0].replace("`", "'").replace("\x00", "")
    sanitized = " ".join(sanitized.split())
    sanitized = sanitized[:SUMMARY_MAX_CHARS].rstrip()
    return sanitized or None
