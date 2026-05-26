"""Pre-flight model management for local model endpoints (e.g., LMStudio).

Ensures the configured model is loaded before spawning a local agent.
Handles model swapping with active-agent conflict detection.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from gobby.config.local import LocalConfig

__all__ = ["ensure_local_model", "LocalModelError"]

logger = logging.getLogger(__name__)


class LocalModelError(Exception):
    """Raised when local model pre-flight fails."""


async def _get_loaded_models(client: httpx.AsyncClient, base_url: str) -> list[dict[str, Any]]:
    """Query the local endpoint for currently loaded models."""
    url = f"{base_url.rstrip('/')}/v1/models"
    resp = await client.get(url, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    result: list[dict[str, Any]] = data.get("data", [])
    return result


async def _load_model(client: httpx.AsyncClient, base_url: str, model: str) -> None:
    """Load a model at the local endpoint."""
    url = f"{base_url.rstrip('/')}/v1/models/load"
    resp = await client.post(url, json={"model": model}, timeout=120.0)
    resp.raise_for_status()
    logger.info(f"Loaded local model: {model}")


async def _unload_model(client: httpx.AsyncClient, base_url: str, model: str) -> None:
    """Unload a model from the local endpoint."""
    url = f"{base_url.rstrip('/')}/v1/models/unload"
    resp = await client.post(url, json={"model": model}, timeout=30.0)
    resp.raise_for_status()
    logger.info(f"Unloaded local model: {model}")


def count_active_local_agents(registry: Any) -> int:
    """Count running agents that were spawned with model: local.

    Args:
        registry: AgentRegistry instance

    Returns:
        Number of active agents using local models
    """
    count = 0
    for run in registry.list_running():
        if getattr(run, "model", None) == "local":
            count += 1
    return count


async def ensure_local_model(
    config: LocalConfig,
    registry: Any | None = None,
) -> str:
    """Ensure the configured model is loaded at the local endpoint.

    Pre-flight check before spawning a local agent:
    - If model is "auto": use whatever is already loaded (no load/unload)
    - If configured model is already loaded → return
    - If different model loaded and no active local agents → swap
    - If different model loaded and active local agents → raise error
    - If no model loaded → load configured model

    Args:
        config: Local model configuration
        registry: Optional AgentRegistry for active-agent checking

    Returns:
        The resolved model name (important for auto mode)

    Raises:
        LocalModelError: If model cannot be loaded or conflict detected
    """
    async with httpx.AsyncClient() as client:
        try:
            loaded = await _get_loaded_models(client, config.url)
        except httpx.ConnectError as e:
            raise LocalModelError(
                f"Cannot connect to local model endpoint at {config.url}. Is LMStudio running?"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LocalModelError(
                f"Local model endpoint returned error: {e.response.status_code}"
            ) from e

        loaded_ids: list[str] = [m.get("id", "") for m in loaded]

        # Auto mode: use whatever is currently loaded, don't manage models
        if config.model == "auto":
            if not loaded_ids:
                raise LocalModelError(
                    "model: auto requires a model to be loaded in LMStudio. "
                    "Load a model in the LMStudio UI first."
                )
            resolved = loaded_ids[0]
            logger.info(f"Auto-detected local model: {resolved}")
            return resolved

        # Check if configured model is already loaded
        if config.model in loaded_ids:
            logger.debug(f"Local model already loaded: {config.model}")
            return config.model

        # Different model (or no model) is loaded
        if loaded_ids:
            # Check for active local agents before swapping
            active_count = count_active_local_agents(registry) if registry else 0
            if active_count > 0:
                raise LocalModelError(
                    f"Cannot swap local model: {active_count} local agent(s) still active "
                    f"using model '{loaded_ids[0]}'. Wait for them to finish."
                )

            # Unload current model(s)
            for model_id in loaded_ids:
                try:
                    await _unload_model(client, config.url, model_id)
                except httpx.HTTPStatusError:
                    logger.warning(f"Failed to unload model: {model_id}")

        # Load configured model
        try:
            await _load_model(client, config.url, config.model)
        except httpx.HTTPStatusError as e:
            raise LocalModelError(
                f"Failed to load model '{config.model}': {e.response.status_code}"
            ) from e
        except httpx.ConnectError as e:
            raise LocalModelError(f"Lost connection to {config.url} while loading model") from e

        return config.model
