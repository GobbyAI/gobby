"""OpenRouter-backed model registry for cost, context, and discovery data.

Replaces LiteLLM's model_cost registry with OpenRouter's public API
(GET https://openrouter.ai/api/v1/models — no auth required).

Data is fetched synchronously at daemon startup (before the event loop)
and persisted to the model_costs DB table. The DB serves as a cache —
if OpenRouter is unreachable, the daemon uses whatever was last fetched.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

import httpx

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
    input_cost_per_token: float
    output_cost_per_token: float
    cache_read_cost_per_token: float | None = None
    cache_creation_cost_per_token: float | None = None


def _parse_pricing(
    pricing: dict[str, str | None] | None,
) -> tuple[float, float, float | None, float | None]:
    """Parse OpenRouter pricing dict (string values) to floats.

    Returns (input, output, cache_read, cache_write). Pricing strings
    are per-token USD (e.g. "0.000005"). Missing or unparseable values
    default to 0.0 for required fields, None for optional cache fields.
    """
    if not pricing:
        return 0.0, 0.0, None, None

    def _to_float(val: str | None) -> float | None:
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    input_cost = _to_float(pricing.get("prompt")) or 0.0
    output_cost = _to_float(pricing.get("completion")) or 0.0
    cache_read = _to_float(pricing.get("input_cache_read"))
    cache_write = _to_float(pricing.get("input_cache_write"))

    return input_cost, output_cost, cache_read, cache_write


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

        input_cost, output_cost, cache_read, cache_write = _parse_pricing(entry.get("pricing"))

        # Skip zero-cost models (free tiers, not useful for cost tracking)
        if input_cost == 0.0 and output_cost == 0.0:
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
                input_cost_per_token=input_cost,
                output_cost_per_token=output_cost,
                cache_read_cost_per_token=cache_read,
                cache_creation_cost_per_token=cache_write,
            )
        )

    logger.info(f"Fetched {len(models)} models from OpenRouter")
    return models


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
