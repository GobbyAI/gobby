"""Provider availability API routes."""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from gobby.servers.provider_models import DROID_MODEL_CATALOG, with_context_lengths

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

# Static model catalog per provider. Dynamic probing can augment this
# later without breaking the contract.
_BASE_MODEL_CATALOG: dict[str, list[dict[str, Any]]] = {
    "claude": with_context_lengths(
        "claude",
        [
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
    "gemini": with_context_lengths(
        "gemini",
        [
            {
                "value": "gemini-3.1-pro-preview",
                "label": "pro-3.1",
                "reasoning": {"supported_efforts": ["low", "medium", "high"]},
            },
            {
                "value": "gemini-3-flash-preview",
                "label": "flash-3",
                "reasoning": {"supported_efforts": ["low", "medium", "high"]},
            },
        ],
    ),
    "qwen": [],
    "codex": with_context_lengths(
        "codex",
        [
            {
                "value": "gpt-5.5",
                "label": "gpt-5.5",
                "reasoning": {"supported_efforts": ["low", "medium", "high", "xhigh"]},
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
}

_PROVIDER_DEFS = [
    ("claude", "claude"),
    ("gemini", "gemini"),
    ("qwen", "qwen"),
    ("codex", "codex"),
    ("droid", "droid"),
]


def _friendly_label(provider: str, model: str) -> str:
    """Return a compact UI label for a provider model ID."""
    if provider == "gemini":
        parts = model.split("-")
        if len(parts) >= 3:
            version = parts[1]
            tier = parts[2]
            return f"{tier}-{version}"
    if provider == "codex":
        if model == "gpt-5.4":
            return "codex-5.4"
        if model == "gpt-5.4-mini":
            return "mini-5.4"
        if model == "gpt-5.3-codex":
            return "codex-5.3"
        if model == "gpt-5.3-codex-spark":
            return "spark-5.3"
    return model


def _configured_model_entries(config: Any, provider: str) -> list[dict[str, Any]]:
    providers = getattr(config, "llm_providers", None)
    provider_config = getattr(providers, provider, None) if providers is not None else None
    fields_set = getattr(providers, "model_fields_set", None)
    if fields_set is not None and provider not in fields_set:
        return []
    if provider_config is None or not hasattr(provider_config, "get_models_list"):
        return []
    return [
        {"value": model, "label": _friendly_label(provider, model)}
        for model in provider_config.get_models_list()
    ]


def _merge_model_entries(
    primary: list[dict[str, Any]], secondary: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_value: dict[str, dict[str, Any]] = {}
    for item in [*primary, *secondary]:
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        if value in by_value:
            existing = by_value[value]
            for key, field_value in item.items():
                existing.setdefault(key, field_value)
            continue
        entry = dict(item)
        by_value[value] = entry
        merged.append(entry)
    return merged


def _build_model_catalog(
    server: HTTPServer | None = None,
) -> dict[str, tuple[list[dict[str, Any]], str]]:
    """Return the canonical web-chat provider model catalog.

    Configured model lists are prepended when present, then live or static
    catalog entries fill in labels, reasoning, context windows, and fallbacks.
    """
    provider_model_catalog = getattr(
        getattr(server, "services", None), "provider_model_catalog", None
    )
    if provider_model_catalog is not None:
        catalog = {}
        for provider, _binary in _PROVIDER_DEFS:
            snapshot = provider_model_catalog.get_provider_snapshot(provider)
            models = snapshot.get("models", [])
            catalog[provider] = (
                with_context_lengths(provider, models) if isinstance(models, list) else [],
                snapshot.get("source", "failed"),
            )
    else:
        catalog = {
            provider: ([*models], "static") for provider, models in _BASE_MODEL_CATALOG.items()
        }

    config = getattr(getattr(server, "services", None), "config", None)
    for provider, (models, source) in list(catalog.items()):
        configured_models = _configured_model_entries(config, provider)
        if configured_models:
            catalog[provider] = (
                with_context_lengths(provider, _merge_model_entries(configured_models, models)),
                source,
            )

    local_cfg = getattr(config, "local", None) if config is not None else None
    if local_cfg and getattr(local_cfg, "model", None):
        claude_entries, source = catalog["claude"]
        if not any(entry["value"] == "local" for entry in claude_entries):
            claude_entries.append({"value": "local", "label": f"Local ({local_cfg.model})"})
        catalog["claude"] = (claude_entries, source)
    return catalog


def _provider_health(
    server: HTTPServer | None,
    provider: str,
    path: str | None,
) -> tuple[bool, str | None]:
    """Resolve provider availability using runtime backend health when available."""
    runtime_manager = getattr(getattr(server, "services", None), "web_chat_runtime_manager", None)
    if runtime_manager is None:
        return path is not None, None

    health = runtime_manager.health(provider)
    if provider == "claude":
        return path is not None and health.available, health.startup_error
    return health.available, health.startup_error


def _filter_models_for_web_chat(
    provider: str, models: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Drop hidden models from the web-chat picker."""
    return [model for model in models if not bool(model.get("hidden", False))]


async def _probe_providers() -> list[tuple[str, str | None]]:
    """Probe provider binaries concurrently.

    Returns a list of (name, path_or_none) tuples.
    """
    paths = await asyncio.gather(
        *[asyncio.to_thread(shutil.which, binary) for _name, binary in _PROVIDER_DEFS]
    )
    return [(name, path) for (name, _binary), path in zip(_PROVIDER_DEFS, paths, strict=False)]


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
                }
            )
        return {"providers": providers}

    @router.get("/models")
    async def list_provider_models() -> dict[str, Any]:
        """Return available models grouped by provider.

        Merges provider availability with the startup-discovered model catalog.
        Falls back to the static catalog when no daemon-backed catalog is present.
        """
        probed = await _probe_providers()
        model_catalog = _build_model_catalog(server)
        fallback_entry: tuple[list[dict[str, Any]], str] = (
            [{"value": "default", "label": "Default"}],
            "static",
        )
        result: list[dict[str, Any]] = []
        for name, path in probed:
            available, startup_error = _provider_health(server, name, path)
            models, source = model_catalog.get(name, fallback_entry)
            filtered_models = _filter_models_for_web_chat(name, models)
            result.append(
                {
                    "provider": name,
                    "available": available,
                    "models": filtered_models,
                    "source": source,
                    "startup_error": startup_error,
                }
            )
        return {"providers": result}

    return router
