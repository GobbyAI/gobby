"""Helpers for determining whether stored model/provider rows represent local models."""

from __future__ import annotations


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def is_local_agent_definition(provider: str | None, model: str | None) -> bool:
    """Detect local model intent from an agent definition or explicit spawn request."""
    return _normalize(provider).startswith("local:") or _normalize(model).startswith("local:")
