"""Provider availability API routes."""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

# Static model catalog per provider. Dynamic probing can augment this
# later without breaking the contract.
_BASE_MODEL_CATALOG: dict[str, list[dict[str, Any]]] = {
    "claude": [
        {
            "value": "opus",
            "label": "Opus",
            "reasoning": {"supported_efforts": ["low", "medium", "high", "max"]},
        },
        {
            "value": "sonnet",
            "label": "Sonnet",
            "reasoning": {"supported_efforts": ["low", "medium", "high", "max"]},
        },
        {
            "value": "haiku",
            "label": "Haiku",
            "reasoning": {"supported_efforts": ["low", "medium", "high", "max"]},
        },
    ],
    "gemini": [
        {"value": "gemini-3.1-pro-preview", "label": "pro-3.1"},
        {"value": "gemini-3-flash-preview", "label": "flash-3"},
    ],
    "qwen": [],
    "codex": [
        {"value": "gpt-5.4", "label": "codex-5.4"},
        {"value": "gpt-5.4-mini", "label": "mini-5.4"},
        {"value": "gpt-5.3-codex", "label": "codex-5.3"},
        {"value": "gpt-5.3-codex-spark", "label": "spark-5.3"},
    ],
}

_PROVIDER_DEFS = [
    ("claude", "claude"),
    ("gemini", "gemini"),
    ("qwen", "qwen"),
    ("codex", "codex"),
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


def _build_model_catalog(
    server: HTTPServer | None = None,
) -> dict[str, tuple[list[dict[str, Any]], str]]:
    """Return the canonical web-chat provider model catalog.

    For web chat, the backend owns the supported model picker contract.
    We intentionally do not mirror arbitrary daemon config model strings into
    the picker because that reintroduces stale or retired model IDs.
    """
    provider_model_catalog = getattr(
        getattr(server, "services", None), "provider_model_catalog", None
    )
    if provider_model_catalog is not None:
        catalog = {
            provider: (
                provider_model_catalog.get_provider_snapshot(provider).get("models", []),
                provider_model_catalog.get_provider_snapshot(provider).get("source", "failed"),
            )
            for provider, _binary in _PROVIDER_DEFS
        }
    else:
        catalog = {
            provider: ([*models], "static") for provider, models in _BASE_MODEL_CATALOG.items()
        }

    config = getattr(getattr(server, "services", None), "config", None)
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
            result.append(
                {
                    "provider": name,
                    "available": available,
                    "models": models,
                    "source": source,
                    "startup_error": startup_error,
                }
            )
        return {"providers": result}

    return router
