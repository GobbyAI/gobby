"""Default title helpers for newly registered sessions."""

from __future__ import annotations

import re

PROVISIONAL_TITLE_SOURCE = "provisional"

_PROVIDER_LABELS = {
    "agy": "agy",
    "claude": "claude",
    "codex": "codex",
    "droid": "droid",
    "grok": "grok",
    "pipeline": "pipeline",
    "qwen": "qwen",
    "unknown": "unknown",
}
_UNKNOWN_PROVIDER_RE = re.compile(r"[^a-z0-9._-]+")


def _normalize_provider_label(source: str) -> str:
    normalized = source.strip().lower()
    if normalized in _PROVIDER_LABELS:
        return _PROVIDER_LABELS[normalized]
    return _UNKNOWN_PROVIDER_RE.sub("-", normalized).strip("-") or "unknown"


def format_provisional_session_title(seq_num: int, source: str) -> str:
    """Return the readable placeholder title used before digest title synthesis."""
    return f"#{seq_num} {_normalize_provider_label(source)}"
