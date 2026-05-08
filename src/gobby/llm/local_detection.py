"""Helpers for determining whether stored model/provider rows represent local models."""

from __future__ import annotations

LOCAL_PROVIDERS = frozenset({"lmstudio", "ollama", "llamacpp", "local"})


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def is_local_legacy_fallback(provider: str | None, model: str | None) -> bool:
    """Best-effort detection for rows created before explicit local flags existed."""
    normalized_provider = _normalize(provider)
    normalized_model = _normalize(model)
    return normalized_provider in LOCAL_PROVIDERS or "gpt-oss" in normalized_model


def is_local_agent_definition(provider: str | None, model: str | None) -> bool:
    """Detect local model intent from an agent definition or explicit spawn request."""
    return _normalize(model) == "local" or is_local_legacy_fallback(provider, model)
