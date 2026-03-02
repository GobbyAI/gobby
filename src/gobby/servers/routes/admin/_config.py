"""Config and model discovery endpoints for admin router."""

import logging
import re
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException

from gobby.utils.metrics import get_metrics_collector
from gobby.utils.version import get_version

if TYPE_CHECKING:
    from gobby.servers.http import HTTPServer

logger = logging.getLogger(__name__)

# Map litellm model prefixes to Gobby provider names
_PROVIDER_PREFIX_MAP: dict[str, str] = {
    "haiku": "claude",
    "gemini": "gemini",
    "gpt": "codex",
    "o1": "codex",
    "o3": "codex",
    "o4": "codex",
}

# Exclude non-coding model categories
_EXCLUDED_KEYWORDS = (
    "audio",
    "image",
    "vision",
    "embedding",
    "realtime",
    "tts",
    "transcribe",
    "search",
    "robotics",
    "live",
    "nano",
    "customtools",
    "computer-use",
    "deep-research",
    "thinking",
    "exp",
)

# Minimum version filters — skip deprecated/retired generations
_MIN_VERSION_FILTERS: dict[str, re.Pattern[str]] = {
    # Gemini: skip 1.x and 2.0 (deprecated)
    "gemini": re.compile(r"^gemini-(1\.|2\.0)"),
    # GPT: skip 3.5, 4, 4o (retired for Codex)
    "codex": re.compile(r"^(gpt-(3\.5|4o|4(?!\.)|4-)|o1)"),
}


def _model_id_to_label(model_id: str) -> str:
    """Convert a model ID like 'gpt-5.3-codex' to 'GPT 5.3 Codex'."""
    # Split on hyphens, title-case each part
    parts = model_id.split("-")
    labelled: list[str] = []
    for part in parts:
        upper = part.upper()
        # Keep well-known acronyms uppercase
        if upper in ("GPT", "O1", "O3", "O4"):
            labelled.append(upper)
        else:
            labelled.append(part.capitalize())
    return " ".join(labelled)


def _discover_models() -> dict[str, list[dict[str, str]]]:
    """Discover models from LiteLLM's model_cost registry.

    Returns models grouped by Gobby provider name, each entry as
    ``{"value": "<model_id>", "label": "<Human Label>"}``.
    Excludes provider-scoped duplicates (containing ``/``), dated
    variants, numeric-suffix duplicates, non-coding categories, and
    very old model generations.
    """
    import litellm

    all_keys = list(litellm.model_cost.keys())

    # Only bare names (no / — those are provider-scoped duplicates like azure/gpt-5)
    bare = [m for m in all_keys if "/" not in m]

    # Exclude dated variants (-YYYYMMDD) and numeric-suffix duplicates (-NNN)
    bare = [m for m in bare if not re.search(r"-\d{6,}", m)]

    # Exclude "latest" aliases
    bare = [m for m in bare if "latest" not in m]

    # Exclude non-coding model categories
    bare = [m for m in bare if not any(kw in m for kw in _EXCLUDED_KEYWORDS)]

    groups: dict[str, list[dict[str, str]]] = {}
    for m in bare:
        # Determine provider
        provider: str | None = None
        for prefix, prov in _PROVIDER_PREFIX_MAP.items():
            if m.startswith(prefix):
                provider = prov
                break
        if provider is None:
            continue

        # Apply minimum-version filter
        version_filter = _MIN_VERSION_FILTERS.get(provider)
        if version_filter and version_filter.search(m):
            continue

        entry = {"value": m, "label": _model_id_to_label(m)}
        groups.setdefault(provider, []).append(entry)

    # Sort each group by value and prepend (default) entry
    result: dict[str, list[dict[str, str]]] = {}
    for provider, entries in sorted(groups.items()):
        sorted_entries = sorted(entries, key=lambda e: e["value"])
        result[provider] = [{"value": "", "label": "(default)"}, *sorted_entries]

    return result


def _fallback_models_from_config(server: "HTTPServer") -> dict[str, list[dict[str, str]]]:
    """Fall back to configured model lists when LiteLLM is unavailable."""
    result: dict[str, list[dict[str, str]]] = {}
    if server.services.config and server.services.config.llm_providers:
        llm_config = server.services.config.llm_providers
        for provider_name in ("claude", "codex", "gemini", "litellm"):
            provider_config = getattr(llm_config, provider_name, None)
            if provider_config:
                models = provider_config.get_models_list()
                if models:
                    entries = [{"value": "", "label": "(default)"}]
                    entries.extend({"value": m, "label": _model_id_to_label(m)} for m in models)
                    result[provider_name] = entries
    return result


def register_config_routes(router: APIRouter, server: "HTTPServer") -> None:
    @router.get("/models")
    async def get_models(provider: str | None = None) -> dict[str, Any]:
        """
        Get available LLM models discovered from LiteLLM's model registry.

        Query params:
            provider: Optional filter (e.g. "claude", "gpt", "gemini")

        Returns:
            Dictionary with models grouped by provider, default_model
        """
        # Determine default model from config or fallback
        default_model = "opus"
        if (
            server.services.config
            and server.services.config.llm_providers
            and server.services.config.llm_providers.default_model
        ):
            default_model = server.services.config.llm_providers.default_model

        # Discover models from LiteLLM registry
        try:
            models_by_provider = _discover_models()
        except Exception as e:
            logger.warning("LiteLLM discovery failed, falling back to config: %s", e)
            # Fallback to config-based models
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
        metrics = get_metrics_collector()
        metrics.inc_counter("http_requests_total")

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
            logger.error("Config retrieval error: %s", e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e
