"""OpenRouter-backed model registry for context window and metadata.

Fetches model data from OpenRouter's public API
(GET https://openrouter.ai/api/v1/models — no auth required).

Data is fetched synchronously at daemon startup (before the event loop)
and persisted to the model_costs DB table. The DB serves as a cache —
if OpenRouter is unreachable, the daemon uses whatever was last fetched.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Maps OpenRouter provider prefixes to Gobby provider names
PROVIDER_MAP: dict[str, str] = {
    "anthropic/": "claude",
    "openai/": "codex",
    "google/": "gemini",
}

# Request timeout — startup shouldn't block forever on a slow network
_FETCH_TIMEOUT = 10.0


@dataclass(frozen=True)
class ModelInfo:
    """Parsed model data from OpenRouter."""

    id: str
    name: str
    provider: str  # Gobby provider name (claude, codex, gemini)
    context_length: int
    max_completion_tokens: int | None


def _provider_for_model(model_id: str) -> str | None:
    """Map an OpenRouter model ID to a Gobby provider name, or None if not relevant."""
    for prefix, provider in PROVIDER_MAP.items():
        if model_id.startswith(prefix):
            return provider
    return None


def fetch_models_sync(timeout: float = _FETCH_TIMEOUT) -> list[ModelInfo]:
    """Fetch models from OpenRouter's public API (sync, no auth).

    Filters to providers in PROVIDER_MAP. Returns empty list on any failure
    (network, parse, timeout) — the caller falls back to cached DB data.
    """
    try:
        response = httpx.get(OPENROUTER_MODELS_URL, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.warning(f"Failed to fetch models from OpenRouter: {e}")
        return []

    if not isinstance(data, dict):
        logger.warning("OpenRouter response is not a dict, skipping")
        return []

    entries = data.get("data", [])
    if not isinstance(entries, list):
        logger.warning("OpenRouter 'data' field is not a list, skipping")
        return []

    models: list[ModelInfo] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        model_id = entry.get("id", "")
        provider = _provider_for_model(model_id)
        if provider is None:
            continue

        top_provider = entry.get("top_provider") or {}
        if not isinstance(top_provider, dict):
            top_provider = {}
        context_length = entry.get("context_length") or 0
        max_completion = top_provider.get("max_completion_tokens")

        models.append(
            ModelInfo(
                id=model_id,
                name=str(entry.get("name", model_id)),
                provider=provider,
                context_length=context_length,
                max_completion_tokens=max_completion,
            )
        )

    logger.info(f"Fetched {len(models)} models from OpenRouter")
    return models


def lookup_context_window(model: str, db: DatabaseProtocol | None = None) -> int | None:
    """Look up context window size for a model.

    Uses ModelCostStore for DB-backed lookup with prefix matching.
    Falls back to the module-level cache if no DB is provided.
    """
    if db is not None:
        from gobby.storage.model_costs import ModelCostStore

        store = ModelCostStore(db)
        return store.get_context_window(model)

    # Fallback: try to get DB from app context
    try:
        from gobby.app_context import get_app_context

        ctx = get_app_context()
        if ctx and ctx.database:
            from gobby.storage.model_costs import ModelCostStore

            store = ModelCostStore(ctx.database)
            return store.get_context_window(model)
    except (ImportError, AttributeError) as e:
        logger.debug(f"App context fallback failed for model {model}: {e}")

    return None


def group_by_provider(models: list[ModelInfo]) -> dict[str, list[ModelInfo]]:
    """Group models by Gobby provider name."""
    grouped: dict[str, list[ModelInfo]] = defaultdict(list)
    for model in models:
        grouped[model.provider].append(model)
    return dict(grouped)


def strip_provider_prefix(model_id: str) -> str:
    """Strip a known OpenRouter provider prefix from a model ID.

    Only strips prefixes that match keys in PROVIDER_MAP (e.g. 'anthropic/',
    'openai/', 'google/'). Unknown prefixes are left intact.

    'anthropic/claude-opus-4-6' -> 'claude-opus-4-6'
    'claude-opus-4-6' -> 'claude-opus-4-6'  (no-op if no prefix)
    'custom/my-model' -> 'custom/my-model'  (unknown prefix, kept)
    """
    if "/" in model_id:
        prefix = model_id.split("/", 1)[0] + "/"
        if prefix in PROVIDER_MAP:
            return model_id.split("/", 1)[1]
    return model_id
