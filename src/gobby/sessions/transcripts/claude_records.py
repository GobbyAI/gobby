"""Normalization helpers for Claude transcript metadata records."""

from typing import Any


def fallback_content(block: dict[str, Any]) -> str:
    """Describe an assistant fallback block."""
    source = block.get("from")
    target = block.get("to")
    source_model = source.get("model") if isinstance(source, dict) else None
    target_model = target.get("model") if isinstance(target, dict) else None
    return f"Model fallback: {source_model or 'unknown'} -> {target_model or 'unknown'}"


def system_event_content(data: dict[str, Any]) -> str | None:
    """Return display text for counted Claude system events."""
    subtype = data.get("subtype")
    if subtype == "api_error":
        error = data.get("error")
        if isinstance(error, dict):
            detail = error.get("formatted") or error.get("message")
        else:
            detail = error
        return str(detail or "API error")
    if subtype == "model_refusal_fallback":
        return str(data.get("content") or "Model refusal fallback")
    return None
