"""Pre-flight model management for local model endpoints.

Ensures the configured model is loaded before spawning a local agent.
Handles model swapping with active-agent conflict detection for LM Studio
and Ollama. vLLM is non-owning: Gobby never load/unload/keep-alive/swap.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from gobby.providers.capabilities.local_context_vllm import (
    parse_vllm_served_records,
    vllm_api_base,
    vllm_health_url,
    vllm_models_url,
)
from gobby.utils.machine_id import get_machine_id, require_machine_id

if TYPE_CHECKING:
    from gobby.config.ai import GenerationEndpointConfig
    from gobby.providers.capabilities.local_context import LocalContextObservation
    from gobby.providers.capabilities.local_context_config import LocalContextRoute
    from gobby.storage.agents import LocalAgentRunManager

__all__ = [
    "ensure_local_model",
    "refresh_local_model_context",
    "LocalModelError",
    "resolve_vllm_served_model",
    "select_vllm_served_model",
    "vllm_api_base",
    "vllm_health_url",
    "vllm_models_url",
    "vllm_served_model_ids",
    "VLLM_TOOL_CALLING_HINT",
]

VLLM_TOOL_CALLING_HINT = (
    "start vLLM with --enable-auto-tool-choice --tool-call-parser <parser> "
    "(hermes for Qwen models), then re-activate the endpoint"
)

logger = logging.getLogger(__name__)


class LocalModelError(Exception):
    """Raised when local model pre-flight fails."""


def _origin(api_base: str) -> str:
    """Return a local endpoint base without a trailing ``/v1`` or slash."""
    base = api_base.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return base


def count_active_local_agents(run_manager: LocalAgentRunManager) -> int:
    """Count running agents that were spawned against a local endpoint.

    Args:
        run_manager: Agent run storage manager.

    Returns:
        Number of active agents using local models
    """
    return sum(
        1 for run in run_manager.list_active_for_machine(require_machine_id()) if run.is_local
    )


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
    """Ensure the configured model is available at the local endpoint.

    Pre-flight check before spawning a local agent:
    - vllm: non-owning GET /v1/models lookup via resolve_vllm_served_model
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

    if config.protocol == "vllm":
        return await resolve_vllm_served_model(config)

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


async def refresh_local_model_context(
    config: GenerationEndpointConfig,
    *,
    endpoint_name: str,
    model: str,
    machine_id: str | None = None,
) -> tuple[LocalContextRoute | None, LocalContextObservation | None]:
    """Refresh context for the resolved local route without changing model lifecycle."""
    from gobby.ai.endpoints import endpoint_provider
    from gobby.app_context import get_app_context
    from gobby.providers.capabilities.local_context_config import endpoint_route

    selected_machine = machine_id or get_machine_id()
    if selected_machine is None:
        logger.warning(
            "Local context refresh skipped because machine identity is unavailable",
            extra={"endpoint": endpoint_name, "model": model},
        )
        return None, None
    route = endpoint_route(
        machine_id=selected_machine,
        endpoint_name=endpoint_name,
        endpoint=config,
        provider=endpoint_provider(endpoint_name),
        model_id=model,
    )
    if not route.is_local:
        return None, None

    try:
        service = getattr(get_app_context(), "local_context_service", None)
        if service is None:
            return route, None
        return route, await service.refresh(route)
    except Exception:
        logger.warning(
            "Local context refresh failed",
            extra={"endpoint": endpoint_name, "model": model},
            exc_info=True,
        )
        return route, None


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


def _vllm_served_model_ids(payload: Any) -> list[str]:
    return parse_vllm_served_records(payload).model_ids()


def _format_served_models(served: list[str]) -> str:
    return ", ".join(served) if served else "(none)"


def select_vllm_served_model(requested: str, served: list[str], *, api_base: str) -> str:
    """Apply the auto-exactly-one / explicit-present rule to a served catalog."""
    requested = requested.strip()
    if requested == "auto":
        if len(served) == 1:
            logger.info("Auto-detected vLLM model: %s", served[0])
            return served[0]
        raise LocalModelError(
            "model: auto requires exactly one served vLLM model; "
            f"found {len(served)}: {_format_served_models(served)}"
        )
    if requested in served:
        return requested
    raise LocalModelError(
        f"vLLM endpoint at {api_base} does not serve model {requested!r}. "
        f"Served models: {_format_served_models(served)}"
    )


async def vllm_served_model_ids(
    api_base: str,
    api_key: str | None,
    *,
    timeout: float = 10.0,
) -> list[str]:
    """Fetch the served model ids from ``GET {origin}/v1/models``."""
    models_url = vllm_models_url(api_base)
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                models_url,
                headers=_headers(api_key),
                timeout=timeout,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except httpx.TimeoutException as e:
            raise LocalModelError(
                f"Timed out waiting for the vllm endpoint at {api_base} to answer GET {models_url}."
            ) from e
        except httpx.RequestError as e:
            raise LocalModelError(
                f"Cannot connect to local vllm endpoint at {api_base}: {e}"
            ) from e
        except httpx.HTTPStatusError as e:
            raise LocalModelError(
                f"Local vllm endpoint returned error: {e.response.status_code}"
            ) from e
        except ValueError as e:
            raise LocalModelError(
                f"vLLM endpoint at {api_base} returned invalid model catalog JSON"
            ) from e

    return _vllm_served_model_ids(payload)


async def resolve_vllm_served_model(endpoint: GenerationEndpointConfig) -> str:
    """Resolve a vLLM endpoint's model id from GET {origin}/v1/models.

    ``model: auto`` maps to the single served model. Zero or multiple served
    models raise ``LocalModelError`` naming them. An explicit model is verified
    present and never loaded. The literal sentinel ``auto`` is never sent on
    the wire.
    """
    served = await vllm_served_model_ids(endpoint.api_base, endpoint.api_key)
    return select_vllm_served_model(endpoint.model, served, api_base=endpoint.api_base)
