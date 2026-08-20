"""Provider availability API routes."""

from __future__ import annotations

import asyncio
import shutil
from collections import Counter
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from gobby.agents.codex_oss import CODEX_OSS_LOCAL_PROVIDERS
from gobby.ai.codex_endpoint import codex_endpoint_display_name
from gobby.ai.endpoint_activation import modalities_for_served_model
from gobby.ai.endpoints import endpoint_provider
from gobby.providers import provider_metadata
from gobby.providers.capabilities.models import ModelCapability, ProviderSnapshot
from gobby.providers.capabilities.resolve import CapabilityResolver, ContextSource
from gobby.servers.local_provider_models import (
    NO_COMPLETION_MODELS_ERROR,
    LocalEndpointModelGroup,
    discover_local_endpoint_model_group,
    local_provider_display_label,
)
from gobby.servers.provider_model_defaults import AGY_MODELS

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

_PROVIDER_DEFS = [(entry.provider, entry.binary) for entry in provider_metadata()]
_PROVIDER_META = {entry.provider: entry for entry in provider_metadata()}
_LAZY_ACP_PROVIDERS = frozenset({"grok", "qwen"})
_GENERIC_LOCAL_UNAVAILABLE_REASON = (
    "Generic OpenAI-compatible endpoints are unavailable for web chat"
)
_CODEX_REQUIRED_REASON = "Codex CLI is required to run local models in web chat"
_CODEX_UNHEALTHY_REASON = "Codex runtime is unavailable"


def _fact(model: ModelCapability, name: str, value: int | None) -> dict[str, Any]:
    provenance = model.provenance.get(name)
    source = provenance.source_key if value is not None and provenance is not None else "unknown"
    return {"value": value, "source": source}


def _context_fact(
    provider: str,
    model: ModelCapability,
    resolver: CapabilityResolver | None,
) -> dict[str, Any]:
    if resolver is None:
        return _fact(model, "context_length", model.context_length)
    resolution = resolver.resolve_context(provider, model.canonical_model)
    if resolution.source is ContextSource.PROVIDER_MATRIX:
        return _fact(model, "context_length", resolution.value)
    if resolution.source is ContextSource.OPENROUTER:
        return {"value": resolution.value, "source": "registry"}
    return {"value": None, "source": "unknown"}


def _matrix_model_entry(
    model: ModelCapability,
    *,
    provider: str,
    resolver: CapabilityResolver | None,
) -> dict[str, Any]:
    route_provenance = {
        name: provenance.to_dict()
        for route in model.routes
        for name, provenance in route.provenance.items()
    }
    return {
        "canonical_model": model.canonical_model,
        "display_name": model.display_name,
        "aliases": list(model.aliases),
        "available": model.available,
        "hidden": model.hidden,
        "is_default": model.is_default,
        "context_length": _context_fact(provider, model, resolver),
        "max_output_tokens": _fact(model, "max_output_tokens", model.max_output_tokens),
        "latency_class": model.latency_class,
        "reasoning": {
            "status": model.reasoning.value,
            "supported_efforts": (
                list(model.supported_efforts) if model.supported_efforts is not None else None
            ),
            "default_effort": model.default_effort,
        },
        "input_modalities": (
            list(model.input_modalities) if model.input_modalities is not None else None
        ),
        "supports_tools": model.supports_tools,
        "routes": {
            route.speed_mode.value: {
                "selector": route.selector,
                "available": route.available,
                "usage_multiplier": (
                    str(route.usage_multiplier) if route.usage_multiplier is not None else None
                ),
                "throughput_multiplier": (
                    str(route.throughput_multiplier)
                    if route.throughput_multiplier is not None
                    else None
                ),
                "latency_class": route.latency_class,
                "activations": [activation.to_dict() for activation in route.activations],
            }
            for route in model.routes
        },
        "provenance": route_provenance,
    }


def _matrix_snapshot_payload(
    snapshot: ProviderSnapshot,
    resolver: CapabilityResolver | None,
) -> dict[str, Any]:
    return {
        "models": [
            _matrix_model_entry(model, provider=snapshot.provider, resolver=resolver)
            for model in snapshot.models
            if not model.hidden
        ],
        "refresh": {
            "generation": snapshot.generation,
            "sources": [source.to_dict() for source in snapshot.sources],
        },
    }


def _pending_snapshot_payload() -> dict[str, Any]:
    return {
        "models": [],
        "refresh": {
            "generation": 0,
            "sources": [{"source_key": "local", "state": "pending"}],
        },
    }


def _agy_snapshot_payload() -> dict[str, Any]:
    return {
        "models": [
            {key: value for key, value in model.items() if key != "effort_display"}
            for model in AGY_MODELS.values()
        ],
        "refresh": {
            "generation": 0,
            "sources": [{"source_key": "static", "state": "ok"}],
        },
    }


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
    codex_available: bool,
    codex_unavailable_reason: str | None,
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
        elif not codex_available:
            unavailable_reason = codex_unavailable_reason or _CODEX_UNHEALTHY_REASON
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
        modalities = modalities_for_served_model(endpoint, endpoint.model)
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
        """Return available models grouped by provider."""
        probed = await _probe_providers()
        capability_service = getattr(
            getattr(server, "services", None),
            "provider_capability_service",
            None,
        )
        capability_resolver = getattr(
            getattr(server, "services", None),
            "provider_capability_resolver",
            None,
        )
        local_model_groups = await _local_generation_model_groups(server)
        result: list[dict[str, Any]] = []
        codex_installed = False
        codex_available = False
        codex_unavailable_reason: str | None = None
        for name, path in probed:
            available, startup_error = _provider_health(server, name, path)
            if name == "agy":
                capability_payload = _agy_snapshot_payload()
            else:
                snapshot = (
                    capability_service.get_provider_snapshot(name)
                    if capability_service is not None
                    else None
                )
                capability_payload = (
                    _matrix_snapshot_payload(snapshot, capability_resolver)
                    if snapshot is not None
                    else _pending_snapshot_payload()
                )
            entry = {
                "provider": name,
                "available": available,
                "startup_error": startup_error,
                **_provider_metadata_fields(name, path),
                **capability_payload,
            }
            if name == "codex":
                codex_installed = path is not None
                codex_available = available
                codex_unavailable_reason = startup_error
                entry["models"] = [
                    *capability_payload["models"],
                    *_responses_endpoint_models(server),
                ]
                entry["execution_provider"] = "codex"
            result.append(entry)
        result.extend(
            _local_generation_provider_entries(
                local_model_groups,
                codex_installed=codex_installed,
                codex_available=codex_available,
                codex_unavailable_reason=codex_unavailable_reason,
            )
        )
        return {"providers": result}

    return router
