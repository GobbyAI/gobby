"""Shared validation for GitHub issue synchronization boundaries."""

from __future__ import annotations

import re

_MAX_GITHUB_ISSUE_NUMBER = 2_147_483_647
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "usage limit",
    "quota exceeded",
    "too many requests",
)
_HTTP_429_PATTERN = re.compile(
    r"\b(?:http(?:\s+(?:response|status))?|status(?:\s+code)?|response\s+code)"
    r"\D{0,12}429\b",
    re.IGNORECASE,
)


def normalize_github_issue_number(value: object) -> int | None:
    """Return a valid GitHub issue number from an external payload value."""
    if type(value) is int:
        number = value
    elif isinstance(value, str) and value.strip().isdecimal():
        number = int(value.strip())
    else:
        return None
    return number if 0 < number <= _MAX_GITHUB_ISSUE_NUMBER else None


def is_github_rate_limit_error(exc: Exception) -> bool:
    """Classify provider failures without confusing issue numbers for HTTP status."""
    if any(getattr(exc, name, None) is not None for name in ("retry_after_seconds", "retry_after")):
        return True
    text = f"{type(exc).__name__}: {exc}"
    normalized = text.casefold()
    return any(marker in normalized for marker in _RATE_LIMIT_MARKERS) or bool(
        _HTTP_429_PATTERN.search(text)
    )
