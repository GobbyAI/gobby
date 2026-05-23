"""Grok model discovery helpers."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

GROK_STATIC_MODEL_CATALOG: list[dict[str, Any]] = [
    {
        "value": "grok-build",
        "label": "Grok Build",
        "description": "Best for advanced coding tasks",
        "context_length": 512_000,
        "reasoning": {"supported_efforts": ["low", "medium", "high"]},
    }
]


def _grok_home() -> Path:
    return Path.home() / ".grok"


def _entry_from_model(model: dict[str, Any]) -> dict[str, Any] | None:
    model_id = str(model.get("modelId") or model.get("id") or model.get("model") or "").strip()
    if not model_id:
        return None
    entry: dict[str, Any] = {
        "value": model_id,
        "label": str(model.get("name") or model.get("label") or model_id),
    }
    description = model.get("description")
    if isinstance(description, str) and description.strip():
        entry["description"] = description.strip()
    meta = model.get("_meta")
    if isinstance(meta, dict):
        context = meta.get("totalContextTokens") or meta.get("context_length")
        if isinstance(context, int) and context > 0:
            entry["context_length"] = context
    return entry


def models_from_acp_session(session_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract Grok model entries from ACP session/init model state."""
    raw_models: Any = []
    models = session_info.get("models")
    if isinstance(models, dict):
        raw_models = models.get("availableModels") or []
    meta = session_info.get("_meta")
    if not raw_models and isinstance(meta, dict):
        model_state = meta.get("modelState")
        if isinstance(model_state, dict):
            raw_models = model_state.get("availableModels") or []

    if not isinstance(raw_models, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        entry = _entry_from_model(item)
        if entry:
            entries.append(entry)
    return entries


def models_from_cache(cache_path: Path | None = None) -> list[dict[str, Any]]:
    """Read ``~/.grok/models_cache.json`` if present."""
    path = cache_path or (_grok_home() / "models_cache.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if isinstance(raw_models, dict):
        raw_models = raw_models.get("availableModels") or raw_models.get("models")
    if not isinstance(raw_models, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        entry = _entry_from_model(item)
        if entry:
            entries.append(entry)
    return entries


def static_models() -> list[dict[str, Any]]:
    """Return Grok static fallback models."""
    return copy.deepcopy(GROK_STATIC_MODEL_CATALOG)
