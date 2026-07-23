"""Pre-flight model management for local model endpoints.

Ensures the configured model is loaded before spawning a local agent.
Handles model swapping with active-agent conflict detection.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from gobby.config.ai import GenerationEndpointConfig
    from gobby.storage.agents import LocalAgentRunManager

__all__ = ["ensure_local_model", "LocalModelError"]

logger = logging.getLogger(__name__)


class LocalModelError(Exception):
    """Raised when local model pre-flight fails."""


def count_active_local_agents(run_manager: LocalAgentRunManager) -> int:
    """Count running agents that were spawned against a local endpoint.

    Args:
        run_manager: Agent run storage manager.

    Returns:
        Number of active agents using local models
    """
    return sum(1 for run in run_manager.list_active() if run.is_local)


def _origin(api_base: str) -> str:
    normalized = api_base.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return normalized


def _headers(api_key: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _lmstudio_model_strings(model: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("key", "display_name", "selected_variant"):
        value = model.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    variants = model.get("variants")
    if isinstance(variants, list):
        candidates.extend(
            str(item).strip() for item in variants if isinstance(item, str) and item.strip()
        )

    loaded_instances = model.get("loaded_instances")
    if isinstance(loaded_instances, list):
        for instance in loaded_instances:
            if not isinstance(instance, dict):
                continue
            value = instance.get("id")
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
    return candidates


def _lmstudio_model_matches(target: str, model: dict[str, Any]) -> bool:
    return target in _lmstudio_model_strings(model)


def _lmstudio_loaded_instance_ids(models: list[dict[str, Any]]) -> list[str]:
    loaded: list[str] = []
    for model in models:
        instances = model.get("loaded_instances")
        if not isinstance(instances, list):
            continue
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            instance_id = instance.get("id")
            if isinstance(instance_id, str) and instance_id.strip():
                loaded.append(instance_id.strip())
    return loaded


async def _lmstudio_models(
    client: httpx.AsyncClient,
    config: GenerationEndpointConfig,
) -> list[dict[str, Any]]:
    response = await client.get(
        f"{_origin(config.api_base)}/api/v1/models",
        headers=_headers(config.api_key),
        timeout=15.0,
    )
    response.raise_for_status()
    payload = response.json()
    models = payload.get("models")
    if not isinstance(models, list):
        raise LocalModelError(f"LM Studio at {config.api_base} returned an invalid model catalog")
    return [model for model in models if isinstance(model, dict)]


async def _load_lmstudio_model(
    client: httpx.AsyncClient,
    config: GenerationEndpointConfig,
    model: str,
) -> None:
    response = await client.post(
        f"{_origin(config.api_base)}/api/v1/models/load",
        headers=_headers(config.api_key),
        json={"model": model},
        timeout=300.0,
    )
    response.raise_for_status()
    logger.info("Loaded LM Studio model: %s", model)


async def _unload_lmstudio_model(
    client: httpx.AsyncClient,
    config: GenerationEndpointConfig,
    instance_id: str,
) -> None:
    response = await client.post(
        f"{_origin(config.api_base)}/api/v1/models/unload",
        headers=_headers(config.api_key),
        json={"instance_id": instance_id},
        timeout=30.0,
    )
    response.raise_for_status()
    logger.info("Unloaded LM Studio model instance: %s", instance_id)


def _ollama_running_model_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    models = payload.get("models")
    if not isinstance(models, list):
        return names
    for model in models:
        if not isinstance(model, dict):
            continue
        for key in ("model", "name"):
            value = model.get(key)
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
                break
    return names


async def _get_ollama_loaded_models(
    client: httpx.AsyncClient,
    config: GenerationEndpointConfig,
) -> list[str]:
    response = await client.get(f"{_origin(config.api_base)}/api/ps", timeout=10.0)
    response.raise_for_status()
    return _ollama_running_model_names(response.json())


async def _set_ollama_keep_alive(
    client: httpx.AsyncClient,
    config: GenerationEndpointConfig,
    model: str,
    keep_alive: int,
) -> None:
    response = await client.post(
        f"{_origin(config.api_base)}/api/chat",
        json={
            "model": model,
            "messages": [],
            "keep_alive": keep_alive,
            "stream": False,
        },
        timeout=300.0,
    )
    response.raise_for_status()


async def ensure_local_model(
    config: GenerationEndpointConfig,
    run_manager: LocalAgentRunManager | None = None,
) -> str:
    """Ensure the configured model is loaded at the local endpoint.

    Pre-flight check before spawning a local agent:
    - If model is "auto": use whatever is already loaded (no load/unload)
    - If configured model is already loaded → return
    - If different model loaded and no active local agents → swap
    - If different model loaded and active local agents → raise error
    - If no model loaded → load configured model

    Args:
        config: Named local generation endpoint configuration
        run_manager: Optional agent run storage manager for active-agent checking.

    Returns:
        The resolved model name (important for auto mode)

    Raises:
        LocalModelError: If model cannot be loaded or conflict detected
    """
    if config.protocol == "openai-compatible":
        if config.model == "auto":
            raise LocalModelError(
                "model: auto requires provider: lmstudio or provider: ollama so Gobby can "
                "inspect loaded local models."
            )
        return config.model

    async with httpx.AsyncClient() as client:
        try:
            if config.protocol == "lmstudio":
                return await _ensure_lmstudio_model(client, config, run_manager)
            if config.protocol == "ollama":
                return await _ensure_ollama_model(client, config, run_manager)
        except httpx.ConnectError as e:
            raise LocalModelError(
                f"Cannot connect to local {config.protocol} endpoint at {config.api_base}."
            ) from e
        except httpx.HTTPStatusError as e:
            raise LocalModelError(
                f"Local {config.protocol} endpoint returned error: {e.response.status_code}"
            ) from e

    raise LocalModelError(f"Unsupported generation endpoint protocol: {config.protocol}")


async def _ensure_lmstudio_model(
    client: httpx.AsyncClient,
    config: GenerationEndpointConfig,
    run_manager: LocalAgentRunManager | None,
) -> str:
    models = await _lmstudio_models(client, config)
    loaded_ids = _lmstudio_loaded_instance_ids(models)

    if config.model == "auto":
        if not loaded_ids:
            raise LocalModelError(
                "model: auto requires a model to be loaded in LM Studio. "
                "Load a model in the LM Studio UI first."
            )
        resolved = loaded_ids[0]
        logger.info("Auto-detected LM Studio model: %s", resolved)
        return resolved

    matched = next(
        (model for model in models if _lmstudio_model_matches(config.model, model)),
        None,
    )
    matched_instances = matched.get("loaded_instances") if isinstance(matched, dict) else None
    matched_loaded = isinstance(matched_instances, list) and any(
        isinstance(instance, dict)
        and isinstance(instance.get("id"), str)
        and instance.get("id") in loaded_ids
        for instance in matched_instances
    )
    if matched_loaded:
        logger.debug("LM Studio model already loaded: %s", config.model)
        return config.model

    if loaded_ids:
        active_count = count_active_local_agents(run_manager) if run_manager else 0
        if active_count > 0:
            raise LocalModelError(
                f"Cannot swap local model: {active_count} local agent(s) still active "
                f"using model '{loaded_ids[0]}'. Wait for them to finish."
            )

        for instance_id in loaded_ids:
            try:
                await _unload_lmstudio_model(client, config, instance_id)
            except httpx.HTTPStatusError:
                logger.warning("Failed to unload LM Studio model instance: %s", instance_id)

    load_model = config.model
    if isinstance(matched, dict):
        key = matched.get("key")
        if isinstance(key, str) and key.strip():
            load_model = key.strip()
    await _load_lmstudio_model(client, config, load_model)
    return config.model


async def _ensure_ollama_model(
    client: httpx.AsyncClient,
    config: GenerationEndpointConfig,
    run_manager: LocalAgentRunManager | None,
) -> str:
    loaded_ids = await _get_ollama_loaded_models(client, config)

    if config.model == "auto":
        if not loaded_ids:
            raise LocalModelError(
                "model: auto requires a model to be loaded in Ollama. "
                "Run `ollama run <model>` first."
            )
        resolved = loaded_ids[0]
        logger.info("Auto-detected Ollama model: %s", resolved)
        return resolved

    if config.model in loaded_ids:
        logger.debug("Ollama model already loaded: %s", config.model)
        return config.model

    if loaded_ids:
        active_count = count_active_local_agents(run_manager) if run_manager else 0
        if active_count > 0:
            raise LocalModelError(
                f"Cannot swap local model: {active_count} local agent(s) still active "
                f"using model '{loaded_ids[0]}'. Wait for them to finish."
            )

        for model_id in loaded_ids:
            try:
                await _set_ollama_keep_alive(client, config, model_id, 0)
            except httpx.HTTPStatusError:
                logger.warning("Failed to unload Ollama model: %s", model_id)

    await _set_ollama_keep_alive(client, config, config.model, -1)
    logger.info("Loaded Ollama model: %s", config.model)
    return config.model
