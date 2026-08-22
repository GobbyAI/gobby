"""OpenRouter-backed model registry for context window and metadata.

Fetches model data from OpenRouter's public API
(GET https://openrouter.ai/api/v1/models — no auth required).

Data is fetched synchronously at daemon startup (before the event loop)
and persisted to the model_metadata DB table. The DB serves as a cache —
if OpenRouter is unreachable, the daemon uses whatever was last fetched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import psycopg
from psycopg_pool import PoolTimeout

from gobby.ai.endpoints import ENDPOINT_PROVIDER_PREFIX
from gobby.llm.context_window_values import positive_context_window

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# LLM vendor prefixes that OpenRouter-style model IDs carry as their first
# slash segment. These describe who published the model, never which Gobby
# provider can run it — metadata is keyed on the model ID alone.
_KNOWN_VENDOR_PREFIXES: frozenset[str] = frozenset(
    {
        "anthropic",
        "openai",
        "moonshotai",
        "z-ai",
        "minimax",
        "qwen",
        "google",
    }
)

# Request timeout — startup shouldn't block forever on a slow network
_FETCH_TIMEOUT = 10.0
_DATABASE_LOOKUP_ERRORS = (psycopg.Error, PoolTimeout)


@dataclass(frozen=True)
class ModelReasoningInfo:
    """Reasoning metadata published by OpenRouter for one model."""

    supported_efforts: tuple[str, ...] | None = None
    default_effort: str | None = None
    default_enabled: bool | None = None
    mandatory: bool | None = None


@dataclass(frozen=True)
class ModelInfo:
    """Parsed model data from OpenRouter."""

    id: str
    name: str
    context_length: int
    max_completion_tokens: int | None
    reasoning: ModelReasoningInfo | None = None


def _parse_reasoning_info(entry: dict[object, object]) -> ModelReasoningInfo | None:
    """Preserve absent reasoning, nullable efforts, and an empty effort list."""
    if "reasoning" not in entry:
        return None
    raw_reasoning = entry.get("reasoning")
    if not isinstance(raw_reasoning, dict):
        logger.warning("OpenRouter model reasoning field is not an object, skipping it")
        return None

    raw_efforts = raw_reasoning.get("supported_efforts")
    supported_efforts: tuple[str, ...] | None = None
    if isinstance(raw_efforts, list):
        supported_efforts = tuple(
            dict.fromkeys(
                effort.strip().lower()
                for effort in raw_efforts
                if isinstance(effort, str) and effort.strip()
            )
        )
    elif raw_efforts is not None:
        logger.warning("OpenRouter supported_efforts field is not a list or null, ignoring it")

    raw_default = raw_reasoning.get("default_effort")
    default_effort = (
        raw_default.strip().lower()
        if isinstance(raw_default, str) and raw_default.strip()
        else None
    )
    raw_default_enabled = raw_reasoning.get("default_enabled")
    raw_mandatory = raw_reasoning.get("mandatory")
    return ModelReasoningInfo(
        supported_efforts=supported_efforts,
        default_effort=default_effort,
        default_enabled=(raw_default_enabled if isinstance(raw_default_enabled, bool) else None),
        mandatory=raw_mandatory if isinstance(raw_mandatory, bool) else None,
    )


def _parse_models_payload(data: object) -> list[ModelInfo]:
    """Parse the shared OpenRouter response shape for sync and async fetches.

    Every entry with a valid context_length is kept — model metadata is
    provider-independent, so models from any vendor enter the catalog.
    Duplicate normalized model IDs keep the larger context window (OpenRouter
    context lengths for one model differ by serving tier; the ceiling avoids
    premature truncation).
    """
    if not isinstance(data, dict):
        logger.warning("OpenRouter response is not a dict, skipping")
        return []

    entries = data.get("data", [])
    if not isinstance(entries, list):
        logger.warning("OpenRouter 'data' field is not a list, skipping")
        return []

    models: dict[str, ModelInfo] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        model_id = entry.get("id", "")

        top_provider = entry.get("top_provider") or {}
        if not isinstance(top_provider, dict):
            top_provider = {}
        context_length = positive_context_window(entry.get("context_length"))
        if context_length is None:
            continue
        max_completion = top_provider.get("max_completion_tokens")

        normalized_id = normalize_model_id(model_id)
        existing = models.get(normalized_id)
        if existing is not None:
            if context_length <= existing.context_length:
                continue
            logger.debug(
                "Model %s appears with differing context lengths (%s vs %s); keeping the larger",
                normalized_id,
                existing.context_length,
                context_length,
            )

        models[normalized_id] = ModelInfo(
            id=model_id,
            name=str(entry.get("name", model_id)),
            context_length=context_length,
            max_completion_tokens=max_completion,
            reasoning=_parse_reasoning_info(entry),
        )

    logger.debug("Fetched %s models from OpenRouter", len(models))
    return list(models.values())


def fetch_models_sync(timeout: float = _FETCH_TIMEOUT) -> list[ModelInfo]:
    """Fetch models from OpenRouter's public API (sync, no auth)."""
    try:
        response = httpx.get(OPENROUTER_MODELS_URL, timeout=timeout)
        response.raise_for_status()
        return _parse_models_payload(response.json())
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.warning("Failed to fetch models from OpenRouter: %s", e)
        return []


async def fetch_models_async(
    timeout: float = _FETCH_TIMEOUT,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[ModelInfo]:
    """Fetch models asynchronously without occupying a worker thread."""
    try:
        if client is not None:
            response = await client.get(OPENROUTER_MODELS_URL, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as owned_client:
                response = await owned_client.get(OPENROUTER_MODELS_URL)
        response.raise_for_status()
        return _parse_models_payload(response.json())
    except (httpx.HTTPError, ValueError, KeyError) as e:
        logger.warning("Failed to fetch models from OpenRouter: %s", e)
        return []


def lookup_context_window(model: str, db: HubDatabase | None = None) -> int | None:
    """Look up context window size for a model.

    Uses ModelMetadataStore for DB-backed lookup with prefix matching.
    Falls back to the module-level cache if no DB is provided.
    """
    if db is not None:
        from gobby.storage.model_metadata import ModelMetadataStore

        store = ModelMetadataStore(db)
        try:
            return store.get_context_window(model)
        except _DATABASE_LOOKUP_ERRORS as exc:
            logger.warning(
                "Catalog context-window database lookup failed for model %s: %s",
                model,
                exc,
            )
            return None

    # Fallback: try to get DB from app context
    try:
        from gobby.app_context import get_app_context
    except ImportError as exc:
        logger.debug("App context fallback failed for model %s: %s", model, exc)
        return None

    ctx = get_app_context()
    if ctx and ctx.database:
        from gobby.storage.model_metadata import ModelMetadataStore

        store = ModelMetadataStore(ctx.database)
        try:
            return store.get_context_window(model)
        except _DATABASE_LOOKUP_ERRORS as exc:
            logger.warning(
                "App-context context-window database lookup failed for model %s: %s",
                model,
                exc,
            )

    return None


def normalize_model_id(model_id: str) -> str:
    """Normalize a model ID to the bare, provider-independent form.

    Strips ``endpoint:<name>/`` routing prefixes first, then strips the first
    slash segment when it is a known LLM vendor prefix. Unknown prefixes are
    left intact.

    'endpoint:fast/anthropic/claude-opus-4-6' -> 'claude-opus-4-6'
    'anthropic/claude-opus-4-6' -> 'claude-opus-4-6'
    'claude-opus-4-6' -> 'claude-opus-4-6'  (no-op if no prefix)
    'custom/my-model' -> 'custom/my-model'  (unknown prefix, kept)
    """
    normalized = model_id.strip()
    if normalized.startswith(ENDPOINT_PROVIDER_PREFIX):
        body = normalized[len(ENDPOINT_PROVIDER_PREFIX) :]
        _endpoint_name, separator, model = body.partition("/")
        if separator and model.strip():
            normalized = model.strip()
    if "/" in normalized:
        prefix, rest = normalized.split("/", 1)
        if prefix.lower() in _KNOWN_VENDOR_PREFIXES:
            return rest
    return normalized
