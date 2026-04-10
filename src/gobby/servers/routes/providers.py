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
_BASE_MODEL_CATALOG: dict[str, list[dict[str, str]]] = {
    "claude": [
        {"value": "opus", "label": "Opus"},
        {"value": "sonnet", "label": "Sonnet"},
        {"value": "haiku", "label": "Haiku"},
    ],
    "gemini": [
        {"value": "gemini-3.1-pro-preview", "label": "pro-3.1"},
        {"value": "gemini-3-flash-preview", "label": "flash-3"},
    ],
    "codex": [
        {"value": "gpt-5.4", "label": "codex-5.4"},
        {"value": "gpt-5.4-mini", "label": "mini-5.4"},
        {"value": "gpt-5.3-codex", "label": "codex-5.3"},
        {"value": "gpt-5.3-codex-spark", "label": "spark-5.3"},
    ],
}

_PROVIDER_DEFS = [("claude", "claude"), ("gemini", "gemini"), ("codex", "codex")]


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
) -> dict[str, tuple[list[dict[str, str]], str]]:
    """Return the canonical web-chat provider model catalog.

    For web chat, the backend owns the supported model picker contract.
    We intentionally do not mirror arbitrary daemon config model strings into
    the picker because that reintroduces stale or retired model IDs.
    """
    catalog = {provider: ([*models], "static") for provider, models in _BASE_MODEL_CATALOG.items()}
    config = getattr(getattr(server, "services", None), "config", None)
    local_cfg = getattr(config, "local", None) if config is not None else None
    if local_cfg and getattr(local_cfg, "model", None):
        claude_entries, source = catalog["claude"]
        if not any(entry["value"] == "local" for entry in claude_entries):
            claude_entries.append({"value": "local", "label": f"Local ({local_cfg.model})"})
        catalog["claude"] = (claude_entries, source)
    return catalog


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
        providers = [
            {"name": name, "available": path is not None, "path": path} for name, path in probed
        ]
        return {"providers": providers}

    @router.get("/models")
    async def list_provider_models() -> dict[str, Any]:
        """Return available models grouped by provider.

        Merges the static catalog with provider availability. Falls back to
        the full catalog when availability probing fails.
        """
        probed = await _probe_providers()
        model_catalog = _build_model_catalog(server)
        result: list[dict[str, Any]] = [
            {
                "provider": name,
                "available": path is not None,
                "models": model_catalog.get(
                    name,
                    ([{"value": "default", "label": "Default"}], "static"),
                )[0],
                "source": model_catalog.get(
                    name,
                    ([{"value": "default", "label": "Default"}], "static"),
                )[1],
            }
            for name, path in probed
        ]
        return {"providers": result}

    return router
