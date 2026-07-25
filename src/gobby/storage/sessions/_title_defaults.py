"""Default title helpers for newly registered sessions."""

from __future__ import annotations

import re

PROVISIONAL_TITLE_SOURCE = "provisional"
DIGEST_TITLE_SOURCE = "llm"
MANUAL_TITLE_SOURCE = "manual"

_PROVIDER_LABELS = {
    "agent-sdk": "Agent SDK",
    "agy": "AGY",
    "claude": "Claude",
    "claude code": "Claude",
    "codex": "Codex",
    "dispatcher_launcher": "Dispatcher",
    "droid": "Droid",
    "grok": "Grok",
    "pipeline": "Pipeline",
    "qwen": "Qwen",
    "unknown": "Unknown",
    "web_launcher": "Web",
}
_UNKNOWN_PROVIDER_RE = re.compile(r"[^a-z0-9._-]+")


def _normalize_provider_label(source: str) -> str:
    normalized = source.strip().lower()
    if normalized in _PROVIDER_LABELS:
        return _PROVIDER_LABELS[normalized]
    fallback = _UNKNOWN_PROVIDER_RE.sub("-", normalized).strip("-")
    if not fallback:
        return _PROVIDER_LABELS["unknown"]
    return re.sub(r"[-_.]+", " ", fallback).title()


def format_provisional_session_title(seq_num: int, source: str) -> str:
    """Return the readable placeholder title used before digest title synthesis."""
    return f"#{seq_num} {_normalize_provider_label(source)}"
