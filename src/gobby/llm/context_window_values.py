"""Strict validation for registry-backed context-window values."""

from __future__ import annotations


def positive_context_window(value: object) -> int | None:
    """Return a positive integer context window, rejecting bools and coercion."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
