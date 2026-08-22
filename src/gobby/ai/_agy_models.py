"""AGY model/effort helpers for text generation."""

from __future__ import annotations

from typing import Any

from gobby.agents.reasoning import normalize_reasoning_effort
from gobby.servers.provider_model_defaults import AGY_MODELS


def _agy_model_entry(model: str) -> dict[str, Any]:
    normalized = _normalize_agy_model_id(model)
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
    normalized_model, normalized_effort = normalize_agy_model_selection(model, effort)
    if normalized_model is None:
        raise ValueError("AGY model is required")
    normalized = normalize_reasoning_effort(normalized_effort)
    if normalized is None or normalized == "auto":
        return agy_default_effort(normalized_model)

    supported = agy_supported_efforts(normalized_model)
    if normalized not in supported:
        accepted = ", ".join(sorted(supported)) or "<none>"
        raise ValueError(
            f"Unsupported AGY reasoning_effort {normalized!r} for model {normalized_model!r}; "
            f"accepted: {accepted}"
        )
    return normalized


def resolve_agy_display(model: str, effort: str | None) -> str:
    """Return the exact AGY --model display string for a model/effort pair."""
    normalized_model, normalized_effort = normalize_agy_model_selection(model, effort)
    if normalized_model is None:
        raise ValueError("AGY model is required")
    resolved = resolve_agy_effort(normalized_model, normalized_effort)
    display_by_effort = _effort_display_map(normalized_model)
    try:
        return display_by_effort[resolved]
    except KeyError as exc:
        raise RuntimeError(
            f"AGY model {normalized_model!r} is missing display string for effort {resolved!r}"
        ) from exc


def _normalize_agy_model_id(model: str) -> str:
    normalized = model.strip()
    alias = _AGY_LEGACY_MODEL_ALIASES.get(normalized.lower())
    return alias[0] if alias is not None else normalized


def normalize_agy_model_selection(
    model: str | None, reasoning_effort: str | None
) -> tuple[str | None, str | None]:
    """Normalize legacy AGY effort-suffixed model values to base ID + effort."""
    if model is None:
        return None, reasoning_effort

    stripped = model.strip()
    alias = _AGY_LEGACY_MODEL_ALIASES.get(stripped.lower())
    if alias is None:
        return stripped, reasoning_effort

    base_model, alias_effort = alias
    if normalize_reasoning_effort(reasoning_effort) in {None, "auto"}:
        return base_model, alias_effort
    return base_model, reasoning_effort


def _build_legacy_agy_model_aliases() -> dict[str, tuple[str, str]]:
    aliases: dict[str, tuple[str, str]] = {}
    for base_model, entry in AGY_MODELS.items():
        effort_display = entry.get("effort_display")
        if not isinstance(effort_display, dict):
            continue
        for effort, display in effort_display.items():
            if not isinstance(effort, str) or not isinstance(display, str):
                continue
            normalized_effort = effort.strip().lower()
            if not normalized_effort:
                continue
            aliases[f"{base_model}-{normalized_effort}".lower()] = (
                base_model,
                normalized_effort,
            )
            aliases[f"{base_model}:{normalized_effort}".lower()] = (
                base_model,
                normalized_effort,
            )
            aliases[display.strip().lower()] = (base_model, normalized_effort)
            suffix = _display_suffix(display)
            if suffix and suffix != normalized_effort:
                aliases[f"{base_model}-{suffix}".lower()] = (base_model, normalized_effort)
    return aliases


def _display_suffix(display: str) -> str | None:
    stripped = display.strip()
    if not stripped.endswith(")") or "(" not in stripped:
        return None
    suffix = stripped.rsplit("(", maxsplit=1)[1][:-1].strip().lower()
    return suffix.replace(" ", "-") or None


_AGY_LEGACY_MODEL_ALIASES = _build_legacy_agy_model_aliases()


__all__ = [
    "agy_default_effort",
    "agy_supported_efforts",
    "normalize_agy_model_selection",
    "resolve_agy_display",
    "resolve_agy_effort",
]
