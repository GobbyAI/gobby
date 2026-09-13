"""Resolve trustworthy reasoning-effort observations across provider payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_EFFECTIVE_KEYS = ("effective_reasoning_effort", "effectiveReasoningEffort")
_FALLBACK_KEYS = (
    "requested_reasoning_effort",
    "requestedReasoningEffort",
    "reasoning_effort",
    "reasoningEffort",
    "model_reasoning_effort",
    "effort",
)
_NESTED_KEYS = ("launch_metadata", "launchMetadata", "config", "configuration")


def observed_reasoning_effort(data: Mapping[str, Any]) -> str | None:
    """Prefer an effective effort, then the requested or configured fallback."""
    sources = [data]
    sources.extend(value for key in _NESTED_KEYS if isinstance((value := data.get(key)), Mapping))
    for keys in (_EFFECTIVE_KEYS, _FALLBACK_KEYS):
        for source in sources:
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None
