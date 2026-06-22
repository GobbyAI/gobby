"""AGY model/effort helpers for text generation."""

from __future__ import annotations

from typing import Any

from gobby.agents.reasoning import normalize_reasoning_effort
from gobby.servers.provider_model_defaults import AGY_MODELS


def _agy_model_entry(model: str) -> dict[str, Any]:
    normalized = model.strip()
    entry = AGY_MODELS.get(normalized)
    if entry is None:
        supported = ", ".join(sorted(AGY_MODELS))
        raise ValueError(f"Unsupported AGY model {model!r}. Supported models: {supported}")
    return entry


def _effort_display_map(model: str) -> dict[str, str]:
    entry = _agy_model_entry(model)
    raw_display = entry.get("effort_display")
    if not isinstance(raw_display, dict):
        raise RuntimeError(f"AGY model {model!r} is missing effort display mappings")

    display_by_effort: dict[str, str] = {}
    for effort, display in raw_display.items():
        if not isinstance(effort, str) or not effort.strip():
            raise RuntimeError(f"AGY model {model!r} has an invalid effort key")
        if not isinstance(display, str) or not display.strip():
            raise RuntimeError(f"AGY model {model!r} has an invalid display string")
        display_by_effort[effort.strip().lower()] = display.strip()
    return display_by_effort


def agy_supported_efforts(model: str) -> frozenset[str]:
    """Return AGY's model-specific effort or mode names."""
    return frozenset(_effort_display_map(model))


def agy_default_effort(model: str) -> str:
    """Return the concrete AGY effort/mode used for auto or omitted effort."""
    entry = _agy_model_entry(model)
    reasoning = entry.get("reasoning")
    default = reasoning.get("default_effort") if isinstance(reasoning, dict) else None
    if not isinstance(default, str) or not default.strip():
        raise RuntimeError(f"AGY model {model!r} is missing a default effort")

    normalized = default.strip().lower()
    if normalized not in agy_supported_efforts(model):
        supported = ", ".join(sorted(agy_supported_efforts(model)))
        raise RuntimeError(
            f"AGY model {model!r} default effort {normalized!r} is not in {supported}"
        )
    return normalized


def resolve_agy_effort(model: str, effort: str | None) -> str:
    """Resolve None/auto to AGY's concrete default and reject invalid pairs."""
    normalized = normalize_reasoning_effort(effort)
    if normalized is None:
        return agy_default_effort(model)

    supported = agy_supported_efforts(model)
    if normalized not in supported:
        accepted = ", ".join(sorted(supported)) or "<none>"
        raise ValueError(
            f"Unsupported AGY reasoning_effort {normalized!r} for model {model!r}; "
            f"accepted: {accepted}"
        )
    return normalized


def resolve_agy_display(model: str, effort: str | None) -> str:
    """Return the exact AGY --model display string for a model/effort pair."""
    resolved = resolve_agy_effort(model, effort)
    display_by_effort = _effort_display_map(model)
    try:
        return display_by_effort[resolved]
    except KeyError as exc:
        raise RuntimeError(
            f"AGY model {model!r} is missing display string for effort {resolved!r}"
        ) from exc


__all__ = [
    "agy_default_effort",
    "agy_supported_efforts",
    "resolve_agy_display",
    "resolve_agy_effort",
]
