"""Provider availability API routes."""

from __future__ import annotations

import asyncio
import copy
import shutil
from collections import Counter
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from gobby.agents.codex_oss import CODEX_OSS_LOCAL_PROVIDERS
from gobby.ai.codex_endpoint import codex_endpoint_display_name
from gobby.ai.endpoints import endpoint_provider
from gobby.providers import provider_metadata
from gobby.servers.local_provider_models import (
    NO_COMPLETION_MODELS_ERROR,
    LocalEndpointModelGroup,
    discover_local_endpoint_model_group,
    local_provider_display_label,
)
from gobby.servers.provider_models import (
    AGY_MODEL_CATALOG,
    DROID_MODEL_CATALOG,
    with_context_lengths,
)
from gobby.servers.provider_models_grok import static_models as grok_static_models

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

# Static model catalog per provider. Dynamic probing can augment this
# later without breaking the contract. Entries may be newer than a reviewer's
# or bot's knowledge cutoff; live discovery is the source of truth, so do not
# remove models here on the grounds that the name is unrecognized.
_BASE_MODEL_CATALOG: dict[str, list[dict[str, Any]]] = {
    "claude": with_context_lengths(
        "claude",
        [
            {
                "value": "fable",
                "label": "Fable",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh", "max"]},
            },
            {
                "value": "opus",
                "label": "Opus",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh", "max"]},
            },
            {
                "value": "sonnet",
                "label": "Sonnet",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh", "max"]},
            },
            {
                "value": "haiku",
                "label": "Haiku",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh", "max"]},
            },
        ],
    ),
    "grok": grok_static_models(),
    "qwen": [],
    "codex": with_context_lengths(
        "codex",
        [
            {
                "value": "gpt-5.6-sol",
                "label": "gpt-5.6-sol",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh"]},
            },
            {
                "value": "gpt-5.6-terra",
                "label": "gpt-5.6-terra",
            },
            {
                "value": "gpt-5.6-luna",
                "label": "gpt-5.6-luna",
            },
            {
                "value": "gpt-5.4",
                "label": "codex-5.4",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh"]},
            },
            {
                "value": "gpt-5.4-mini",
                "label": "mini-5.4",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh"]},
            },
            {
                "value": "gpt-5.3-codex",
                "label": "codex-5.3",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh"]},
            },
            {
                "value": "gpt-5.3-codex-spark",
                "label": "spark-5.3",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh"]},
            },
            {
                "value": "gpt-5.2",
                "label": "gpt-5.2",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh"]},
            },
        ],
    ),
    "droid": DROID_MODEL_CATALOG,
    "agy": AGY_MODEL_CATALOG,
}

_PROVIDER_DEFS = [(entry.provider, entry.binary) for entry in provider_metadata()]
_PROVIDER_META = {entry.provider: entry for entry in provider_metadata()}
_LAZY_ACP_PROVIDERS = frozenset({"grok", "qwen"})
_GENERIC_LOCAL_UNAVAILABLE_REASON = (
    "Generic OpenAI-compatible endpoints are unavailable for web chat"
)
_CODEX_REQUIRED_REASON = "Codex CLI is required to run local models in web chat"


def _merge_static_model_metadata(
    models: list[dict[str, Any]],
    static_models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    static_by_value = {
        str(model.get("value") or "").strip(): model
        for model in static_models
        if str(model.get("value") or "").strip()
    }
    merged: list[dict[str, Any]] = []
    for model in models:
        value = str(model.get("value") or "").strip()
        entry = copy.deepcopy(model)
        static_entry = static_by_value.get(value)
        if static_entry:
            for key, field_value in static_entry.items():
                if key not in entry:
                    entry[key] = copy.deepcopy(field_value)
        merged.append(entry)
    return merged


def _build_model_catalog(
    server: HTTPServer | None = None,
) -> dict[str, tuple[list[dict[str, Any]], str]]:
    """Return the canonical web-chat provider model catalog.

    Live discovery owns model availability when present; static catalog entries
    fill in labels, reasoning, context windows, and fallbacks.
    """
    provider_model_catalog = getattr(
        getattr(server, "services", None), "provider_model_catalog", None
    )
    if provider_model_catalog is not None:
        catalog: dict[str, tuple[list[dict[str, Any]], str]] = {}
        for provider, _binary in _PROVIDER_DEFS:
            meta = _PROVIDER_META[provider]
            static_models = _BASE_MODEL_CATALOG.get(provider, [])
            if not meta.live_model_discovery:
                source = "static" if static_models else "unsupported"
                catalog[provider] = ([*static_models], source)
                continue
            snapshot = provider_model_catalog.get_provider_snapshot(provider)
            models = snapshot.get("models", [])
            source = snapshot.get("source", "failed")
            if isinstance(models, list) and models:
                catalog[provider] = (
                    with_context_lengths(
                        provider, _merge_static_model_metadata(models, static_models)
                    ),
                    source,
                )
            elif static_models:
                catalog[provider] = ([*static_models], "static")
            else:
                catalog[provider] = ([], source)
    else:
        catalog = {
            provider: (
                [*models],
                "static"
                if models or _PROVIDER_META[provider].live_model_discovery
                else "unsupported",
            )
            for provider, models in _BASE_MODEL_CATALOG.items()
        }

    return catalog


async def _local_generation_model_groups(
    server: HTTPServer | None,
) -> list[LocalEndpointModelGroup]:
    return list(
        await asyncio.gather(
            *(
                discover_local_endpoint_model_group(name, endpoint)
                for name, endpoint in _configured_endpoints(server, "chat-completions")
            )
        )
    )


def _local_generation_provider_entries(
    groups: list[LocalEndpointModelGroup],
    *,
    codex_installed: bool,
) -> list[dict[str, Any]]:
    provider_type_counts = Counter(group.provider_type for group in groups)
    entries: list[dict[str, Any]] = []
    for group in groups:
        # LM Studio/Ollama chat-completions endpoints execute through the
        # Codex OSS runtime (see WebChatRuntimeManager); generic
        # OpenAI-compatible endpoints have no web-chat transport (#19161).
        routable = group.provider_type in CODEX_OSS_LOCAL_PROVIDERS
        if not routable:
            unavailable_reason: str | None = _GENERIC_LOCAL_UNAVAILABLE_REASON
        elif group.error:
            unavailable_reason = group.error
        elif not group.models:
            unavailable_reason = NO_COMPLETION_MODELS_ERROR
        elif not codex_installed:
            unavailable_reason = _CODEX_REQUIRED_REASON
        else:
            unavailable_reason = None
        available = unavailable_reason is None
        display_name = group.provider_label
        if provider_type_counts[group.provider_type] > 1:
            display_name = f"{display_name} ({group.endpoint_name})"
        entry: dict[str, Any] = {
            "provider": group.provider,
            "available": available,
            "models": group.models,
            "source": group.source,
            "startup_error": group.error,
            "display_name": display_name,
            "provider_type": group.provider_type,
            "installed": True,
            "deprecated": False,
            "deprecation_message": None,
            "supports_web_chat": available,
            "supports_agent_spawn": False,
            "unavailable_reason": unavailable_reason,
        }
        if available:
            entry["execution_provider"] = "codex"
        entries.append(entry)
    return entries


def _provider_health(
    server: HTTPServer | None,
    provider: str,
    path: str | None,
) -> tuple[bool, str | None]:
    """Resolve provider availability using runtime backend health when available."""
    meta = _PROVIDER_META.get(provider)
    if meta and not meta.supports_web_chat:
        return False, meta.unavailable_reason

    runtime_manager = getattr(getattr(server, "services", None), "web_chat_runtime_manager", None)
    if runtime_manager is None:
        return path is not None, None

    health = runtime_manager.health(provider)
    if provider == "claude":
        return path is not None and health.available, health.startup_error
    if provider in _LAZY_ACP_PROVIDERS:
        return path is not None, health.startup_error
    return health.available, health.startup_error


def _filter_models_for_web_chat(
    provider: str, models: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop hidden models from the web-chat picker.

    Only the provider-reported ``hidden`` flag may exclude a model here. Do not
    re-add value-based blocklists for models a reviewer or bot does not
    recognize; live discovery is the source of truth.
    """
    return [model for model in models if not bool(model.get("hidden", False))]


async def _probe_providers() -> list[tuple[str, str | None]]:
    """Probe provider binaries concurrently.

    Returns a list of (name, path_or_none) tuples.
    """
    paths = await asyncio.gather(
        *[asyncio.to_thread(shutil.which, meta.binary) for meta in provider_metadata()]
    )
    return [(name, path) for (name, _binary), path in zip(_PROVIDER_DEFS, paths, strict=False)]


def _provider_metadata_fields(name: str, path: str | None) -> dict[str, Any]:
    meta = _PROVIDER_META[name]
    return {
        "display_name": meta.display_name,
        "installed": path is not None,
        "deprecated": meta.deprecated,
        "deprecation_message": meta.deprecation_message,
        "supports_web_chat": meta.supports_web_chat,
        "supports_agent_spawn": meta.supports_agent_spawn,
        "unavailable_reason": meta.unavailable_reason,
    }


def _configured_endpoints(
    server: HTTPServer | None,
    wire_api: str,
) -> Iterator[tuple[str, Any]]:
    config = getattr(getattr(server, "services", None), "config", None)
    generation = getattr(getattr(config, "ai", None), "generation", None)
    endpoints = getattr(generation, "endpoints", {})
    if not isinstance(endpoints, dict):
        return
    for endpoint_name, endpoint in endpoints.items():
        if getattr(endpoint, "wire_api", None) == wire_api:
            yield endpoint_name, endpoint


def _configured_endpoint_provider_entries(server: HTTPServer | None) -> list[dict[str, Any]]:
    """Return provider rows for Chat Completions generation endpoints."""
    entries: list[dict[str, Any]] = []
    for endpoint_name, endpoint in _configured_endpoints(server, "chat-completions"):
        provider_type = str(getattr(endpoint, "protocol", "openai-compatible"))
        entries.append(
            {
                "name": endpoint_provider(endpoint_name),
                "available": False,
                "path": None,
                "startup_error": None,
                "display_name": f"Local: {local_provider_display_label(provider_type)}",
                "installed": True,
                "deprecated": False,
                "deprecation_message": None,
                "supports_web_chat": False,
                "supports_agent_spawn": False,
                "unavailable_reason": _GENERIC_LOCAL_UNAVAILABLE_REASON,
            }
        )
    return entries


def _responses_endpoint_models(server: HTTPServer | None) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    for endpoint_name, endpoint in _configured_endpoints(server, "responses"):
        modalities = ["text", "image"] if endpoint.vision_extract else ["text"]
        models.append(
            {
                "value": f"{endpoint_provider(endpoint_name)}/{endpoint.model}",
                "label": (f"{codex_endpoint_display_name(endpoint_name)}: {endpoint.model}"),
                "canonical_id": endpoint.model,
                "input_modalities": modalities,
                "supports_tools": endpoint.tool_chat,
                "execution_provider": "codex",
            }
        )
    return models


def create_providers_router(server: HTTPServer | None = None) -> APIRouter:
    """Create providers API router.

    Returns:
        Configured APIRouter.
    """
    router = APIRouter(prefix="/api/providers", tags=["providers"])

    @router.get("")
    async def list_providers() -> dict[str, Any]:
        """List CLI providers with their availability status."""
        probed = await _probe_providers()
        providers = []
        for name, path in probed:
            available, startup_error = _provider_health(server, name, path)
            providers.append(
                {
                    "name": name,
                    "available": available,
                    "path": path,
                    "startup_error": startup_error,
                    **_provider_metadata_fields(name, path),
                }
            )
        providers.extend(_configured_endpoint_provider_entries(server))
        return {"providers": providers}

    @router.get("/models")
    async def list_provider_models() -> dict[str, Any]:
        """Return available models grouped by provider.

        Merges provider availability with the startup-discovered model catalog.
        Falls back to the static catalog when no daemon-backed catalog is present.
        """
        probed = await _probe_providers()
        model_catalog = _build_model_catalog(server)
        local_model_groups = await _local_generation_model_groups(server)
        fallback_entry: tuple[list[dict[str, Any]], str] = (
            [{"value": "default", "label": "Default"}],
            "static",
        )
        result: list[dict[str, Any]] = []
        for name, path in probed:
            available, startup_error = _provider_health(server, name, path)
            models, source = model_catalog.get(name, fallback_entry)
            filtered_models = _filter_models_for_web_chat(name, models)
            entry = {
                "provider": name,
                "available": available,
                "models": filtered_models,
                "source": source,
                "startup_error": startup_error,
                **_provider_metadata_fields(name, path),
            }
            if name == "codex":
                entry["models"] = [*filtered_models, *_responses_endpoint_models(server)]
                entry["execution_provider"] = "codex"
            result.append(entry)
        codex_installed = any(name == "codex" and path is not None for name, path in probed)
        result.extend(
            _local_generation_provider_entries(local_model_groups, codex_installed=codex_installed)
        )
        return {"providers": result}

    return router
