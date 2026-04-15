"""Config and model discovery endpoints for admin router."""

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from gobby.utils.version import get_version

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)


def _discover_models() -> dict[str, list[dict[str, str]]]:
    """Discover models from OpenRouter's public API.

    Returns models grouped by Gobby provider name, each entry as
    ``{"value": "<model_id>", "label": "<Human Label>"}``.
    """
    from gobby.llm.model_registry import fetch_models_sync, group_by_provider, strip_provider_prefix

    models = fetch_models_sync()
    if not models:
        return {}

    grouped = group_by_provider(models)

    result: dict[str, list[dict[str, str]]] = {}
    for provider, model_list in sorted(grouped.items()):
        entries = [{"value": "", "label": "(default)"}]
        for m in sorted(model_list, key=lambda x: x.id):
            entries.append({"value": strip_provider_prefix(m.id), "label": m.name})
        result[provider] = entries

    return result


def _fallback_models_from_config(server: "HTTPServer") -> dict[str, list[dict[str, str]]]:
    """Fall back to configured model lists when OpenRouter is unavailable."""
    result: dict[str, list[dict[str, str]]] = {}
    if server.services.config and server.services.config.llm_providers:
        llm_config = server.services.config.llm_providers
        for provider_name in ("claude", "codex", "gemini", "qwen"):
            provider_config = getattr(llm_config, provider_name, None)
            if provider_config:
                models = provider_config.get_models_list()
                if models:
                    entries: list[dict[str, str]] = [{"value": "", "label": "(default)"}]
                    entries.extend({"value": m, "label": m} for m in models)
                    result[provider_name] = entries
    return result


def register_config_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/models")
    async def get_models(provider: str | None = None) -> dict[str, Any]:
        """
        Get available LLM models discovered from OpenRouter's model registry.

        Query params:
            provider: Optional filter (e.g. "claude", "codex", "gemini", "qwen")

        Returns:
            Dictionary with models grouped by provider, default_model
        """
        # Determine default model from config
        default_model = (
            server.services.config.llm_providers.default_model
            if server.services.config and server.services.config.llm_providers
            else None
        )

        # Discover models from OpenRouter registry
        try:
            models_by_provider = await asyncio.to_thread(_discover_models)
        except Exception as e:
            logger.warning(f"Model discovery failed, falling back to config: {e}")
            models_by_provider = _fallback_models_from_config(server)

        # Apply provider filter
        if provider:
            filtered = {k: v for k, v in models_by_provider.items() if k == provider}
            models_by_provider = filtered

        return {
            "models": models_by_provider,
            "default_model": default_model,
        }

    @router.get("/config")
    async def get_config() -> dict[str, Any]:
        """
        Get daemon configuration and version information.

        Returns:
            Configuration data including ports, features, and versions
        """
        start_time = time.perf_counter()

        try:
            config_data = {
                "server": {
                    "port": server.port,
                    "test_mode": server.test_mode,
                    "running": server._running,
                    "version": get_version(),
                },
                "features": {
                    "session_manager": server.session_manager is not None,
                    "mcp_manager": server.mcp_manager is not None,
                },
                "endpoints": {
                    "mcp": [
                        "/api/mcp/{server_name}/tools/{tool_name}",
                    ],
                    "sessions": [
                        "/api/sessions/register",
                        "/api/sessions/{id}",
                    ],
                    "admin": [
                        "/api/admin/status",
                        "/api/admin/metrics",
                        "/api/admin/config",
                        "/api/admin/shutdown",
                    ],
                },
            }

            response_time_ms = (time.perf_counter() - start_time) * 1000

            return {
                "status": "success",
                "config": config_data,
                "response_time_ms": response_time_ms,
            }

        except Exception as e:
            logger.error(f"Config retrieval error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
