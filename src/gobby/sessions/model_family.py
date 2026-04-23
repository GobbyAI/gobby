"""Helpers for normalizing raw model identifiers into stable families."""

from __future__ import annotations

import re

_KNOWN_PROVIDER_PREFIXES = frozenset(
    {
        "anthropic",
        "openai",
        "google",
        "google-ai-studio",
        "vertex",
        "vertex-ai",
    }
)
_TRAILING_AUTH = re.compile(r"\([^)]*\)$")
_TRAILING_DATE = re.compile(r"-(?:19|20)\d{6}$")
_TRAILING_PREVIEW = re.compile(r"-(?:preview(?:-\d{2}-\d{2})?|experimental|exp|latest)$")


def normalize_model(model: str | None) -> str | None:
    """Collapse raw model identifiers into stable family labels.

    Examples:
        ``claude-sonnet-4-5-20250929`` -> ``claude-sonnet-4-5``
        ``gemini-2.5-pro-preview`` -> ``gemini-2.5-pro``
        ``anthropic/claude-opus-4-6-20260101`` -> ``claude-opus-4-6``
        ``gpt-5(openai)`` -> ``gpt-5``
    """
    if not isinstance(model, str):
        return None

    normalized = model.strip().lower()
    if not normalized:
        return None

    if "/" in normalized:
        prefix, remainder = normalized.split("/", 1)
        if prefix in _KNOWN_PROVIDER_PREFIXES and remainder:
            normalized = remainder

    normalized = _TRAILING_AUTH.sub("", normalized).strip()

    previous = None
    while normalized and normalized != previous:
        previous = normalized
        normalized = _TRAILING_DATE.sub("", normalized)
        normalized = _TRAILING_PREVIEW.sub("", normalized)

    normalized = normalized.rstrip("-")
    return normalized or None
