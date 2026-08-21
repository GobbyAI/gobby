"""Model discovery for configured local generation endpoints."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from gobby.agents.local_model import (
    LocalModelError,
    select_vllm_served_model,
    vllm_health_url,
    vllm_models_url,
)
from gobby.ai.endpoints import ENDPOINT_PROVIDER_PREFIX
from gobby.config.ai import GenerationEndpointConfig, GenerationEndpointProtocol

LOCAL_PROVIDER_LABELS: dict[str, str] = {
    "lmstudio": "LM Studio",
    "ollama": "Ollama",
    "openai-compatible": "OpenAI Compatible",
    "vllm": "vLLM",
}
NO_COMPLETION_MODELS_ERROR = "No completion-capable models discovered"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalEndpointModelGroup:
    """Discovered model rows for one configured local endpoint."""

    endpoint_name: str
    provider_type: GenerationEndpointProtocol
    provider_label: str
    models: list[dict[str, Any]]
    source: str
    error: str | None = None
    probed_tools: bool | None = None

    @property
    def provider(self) -> str:
        return f"{ENDPOINT_PROVIDER_PREFIX}{self.endpoint_name}"

    @property
    def display_name(self) -> str:
        return f"Local: {self.provider_label}"


async def discover_local_endpoint_model_group(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
) -> LocalEndpointModelGroup:
    """Discover selectable models for one configured local generation endpoint."""
    provider_label = local_provider_display_label(endpoint.protocol)
    try:
        if endpoint.protocol == "lmstudio":
            discovered = await _discover_lmstudio_models(endpoint_name, endpoint)
        elif endpoint.protocol == "ollama":
            discovered = await _discover_ollama_models(endpoint_name, endpoint)
        elif endpoint.protocol == "vllm":
            discovered = await _discover_vllm_models(endpoint_name, endpoint)
        else:
            async with httpx.AsyncClient() as client:
                discovered = await _openai_compatible_models(client, endpoint_name, endpoint)
        models = _merge_default_model(endpoint_name, endpoint, discovered)
        capability_checked = endpoint.protocol in {"lmstudio", "ollama", "vllm"}
        source = "live" if discovered or capability_checked else "config"
        return LocalEndpointModelGroup(
            endpoint_name=endpoint_name,
            provider_type=endpoint.protocol,
            provider_label=provider_label,
            models=models,
            source=source,
            error=NO_COMPLETION_MODELS_ERROR if capability_checked and not models else None,
            probed_tools=endpoint.probed_tools,
        )
    except Exception as exc:
        logger.warning(
            "Model discovery failed for endpoint %r: %s; falling back to configured models",
            endpoint_name,
            _short_error(exc),
        )
        return LocalEndpointModelGroup(
            endpoint_name=endpoint_name,
            provider_type=endpoint.protocol,
            provider_label=provider_label,
            models=_merge_default_model(endpoint_name, endpoint, []),
            source="config",
            error=_short_error(exc),
            probed_tools=endpoint.probed_tools,
        )


def local_provider_display_label(provider: str) -> str:
    normalized = provider.strip().lower()
    return LOCAL_PROVIDER_LABELS.get(normalized, provider.strip() or "Local")


def _default_alias_canonical_id(
    endpoint: GenerationEndpointConfig,
    discovered: list[dict[str, Any]],
) -> str | None:
    if endpoint.protocol == "vllm":
        served = [
            canonical_id
            for entry in discovered
            if isinstance(canonical_id := entry.get("canonical_id"), str) and canonical_id.strip()
        ]
        try:
            return select_vllm_served_model(endpoint.model, served, api_base=endpoint.api_base)
        except LocalModelError:
            return None
    if endpoint.protocol == "openai-compatible":
        return endpoint.model
    if any(entry.get("canonical_id") == endpoint.model for entry in discovered):
        return endpoint.model
    return None


def _merge_default_model(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    discovered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    alias_id = _default_alias_canonical_id(endpoint, discovered)
    entries: list[dict[str, Any]] = []
    if alias_id is not None:
        default_entry: dict[str, Any] = {
            "value": f"{ENDPOINT_PROVIDER_PREFIX}{endpoint_name}",
            "label": f"Default ({endpoint.model})",
            "canonical_id": endpoint.model,
            "is_default": True,
        }
        for entry in discovered:
            if entry.get("canonical_id") != alias_id:
                continue
            modalities = entry.get("input_modalities")
            if modalities is not None:
                default_entry["input_modalities"] = modalities
            break
        entries.append(default_entry)
    entries.extend(discovered)
    deduped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        value = str(entry.get("value") or "").strip()
        if value and value not in deduped:
            deduped[value] = entry
    return list(deduped.values())


async def _discover_lmstudio_models(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{_origin(endpoint.api_base)}/api/v1/models",
            headers=_headers(endpoint.api_key),
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    models = _model_list(payload)
    entries: list[dict[str, Any]] = []
    for model in models:
        if not _is_lmstudio_llm(model):
            continue
        model_id = _first_string(model, "id", "key", "model")
        if model_id is None:
            continue
        entries.append(
            _local_model_entry(
                endpoint_name,
                model_id,
                label=_first_string(model, "display_name", "name", "id", "key") or model_id,
                context_length=_context_length(model),
                capabilities=_capabilities(model),
                input_modalities=_entry_input_modalities(
                    endpoint,
                    model_id,
                    _lmstudio_advertised_modalities(model),
                ),
            )
        )
    return entries


async def _discover_ollama_models(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
) -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        native_available, native_models = await _ollama_native_models(client, endpoint)
        if native_models:
            return [
                _local_model_entry(
                    endpoint_name,
                    model_id,
                    label=_ollama_label(model, model_id),
                    context_length=_context_length(model),
                    capabilities=_ollama_capabilities(model),
                    input_modalities=_entry_input_modalities(
                        endpoint,
                        model_id,
                        _ollama_advertised_modalities(model),
                    ),
                )
                for model, model_id in native_models
            ]
        try:
            fallback_models = await _openai_compatible_models(client, endpoint_name, endpoint)
        except httpx.HTTPError:
            if native_available:
                return []
            raise
        return await _validated_ollama_fallback_models(client, endpoint, fallback_models)


async def _ollama_native_models(
    client: httpx.AsyncClient,
    endpoint: GenerationEndpointConfig,
) -> tuple[bool, list[tuple[dict[str, Any], str]]]:
    models_by_id: dict[str, dict[str, Any]] = {}
    native_available = False
    for path in ("/api/tags", "/api/ps"):
        try:
            response = await client.get(
                f"{_origin(endpoint.api_base)}{path}",
                headers=_headers(endpoint.api_key),
                timeout=10.0,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        native_available = True
        for model in _model_list(response.json()):
            model_id = _first_string(model, "model", "name")
            if model_id:
                models_by_id.setdefault(model_id, model)
    models_by_id.setdefault(endpoint.model, {"name": endpoint.model})

    eligible: list[tuple[dict[str, Any], str]] = []
    model_items = list(models_by_id.items())
    detail_results = await _ollama_model_details_batch(
        client, endpoint, [model_id for model_id, _ in model_items]
    )
    for (model_id, model), (show_available, details) in zip(
        model_items, detail_results, strict=True
    ):
        native_available = native_available or show_available
        if not _supports_ollama_completion(details):
            continue
        eligible.append(({**model, **details}, model_id))
    return native_available, eligible


async def _validated_ollama_fallback_models(
    client: httpx.AsyncClient,
    endpoint: GenerationEndpointConfig,
    models: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    model_items = [
        (model, model_id)
        for model in models
        if (model_id := _first_string(model, "canonical_id")) is not None
    ]
    detail_results = await _ollama_model_details_batch(
        client, endpoint, [model_id for _, model_id in model_items]
    )
    eligible: list[dict[str, Any]] = []
    for (model, model_id), (_, details) in zip(model_items, detail_results, strict=True):
        if not _supports_ollama_completion(details):
            continue
        modalities = _entry_input_modalities(
            endpoint,
            model_id,
            _ollama_advertised_modalities(details),
        )
        if modalities is None:
            eligible.append(model)
        else:
            eligible.append({**model, "input_modalities": modalities})
    return eligible


async def _ollama_model_details_batch(
    client: httpx.AsyncClient,
    endpoint: GenerationEndpointConfig,
    model_ids: list[str],
) -> list[tuple[bool, dict[str, Any]]]:
    semaphore = asyncio.Semaphore(4)

    async def fetch(model_id: str) -> tuple[bool, dict[str, Any]]:
        async with semaphore:
            return await _ollama_model_details(client, endpoint, model_id)

    return list(await asyncio.gather(*(fetch(model_id) for model_id in model_ids)))


async def _ollama_model_details(
    client: httpx.AsyncClient,
    endpoint: GenerationEndpointConfig,
    model_id: str,
) -> tuple[bool, dict[str, Any]]:
    try:
        response = await client.post(
            f"{_origin(endpoint.api_base)}/api/show",
            headers=_headers(endpoint.api_key),
            json={"model": model_id},
            timeout=10.0,
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return False, {}
    payload = response.json()
    return True, payload if isinstance(payload, dict) else {}


def _supports_ollama_completion(model: dict[str, Any]) -> bool:
    capabilities = model.get("capabilities")
    return isinstance(capabilities, list) and any(
        isinstance(capability, str) and capability.lower() == "completion"
        for capability in capabilities
    )


async def _discover_vllm_models(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
) -> list[dict[str, Any]]:
    models_url = vllm_models_url(endpoint.api_base)
    health_url = vllm_health_url(endpoint.api_base)
    headers = _headers(endpoint.api_key)
    async with httpx.AsyncClient() as client:
        health = await client.get(health_url, headers=headers, timeout=10.0)
        health.raise_for_status()
        response = await client.get(models_url, headers=headers, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    entries: list[dict[str, Any]] = []
    for model in _model_list(payload):
        model_id = _first_string(model, "id", "model", "name")
        if model_id is None:
            continue
        entries.append(
            _local_model_entry(
                endpoint_name,
                model_id,
                label=_first_string(model, "label", "name", "id") or model_id,
                context_length=_context_length(model),
                capabilities=_capabilities(model),
                input_modalities=_entry_input_modalities(endpoint, model_id, None),
            )
        )
    return entries


async def _openai_compatible_models(
    client: httpx.AsyncClient,
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
) -> list[dict[str, Any]]:
    response = await client.get(
        f"{_origin(endpoint.api_base)}/v1/models",
        headers=_headers(endpoint.api_key),
        timeout=10.0,
    )
    response.raise_for_status()
    entries: list[dict[str, Any]] = []
    for model in _model_list(response.json()):
        model_id = _first_string(model, "id", "model", "name")
        if model_id:
            entries.append(
                _local_model_entry(
                    endpoint_name,
                    model_id,
                    label=_first_string(model, "label", "name", "id") or model_id,
                    context_length=_context_length(model),
                    capabilities=_capabilities(model),
                )
            )
    return entries


def _local_model_entry(
    endpoint_name: str,
    model_id: str,
    *,
    label: str,
    context_length: int | None,
    capabilities: Any | None,
    input_modalities: list[str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "value": f"{ENDPOINT_PROVIDER_PREFIX}{endpoint_name}/{model_id}",
        "label": label,
        "canonical_id": model_id,
    }
    if context_length is not None:
        entry["context_length"] = context_length
        entry["context_length_source"] = "provider_reported"
    if capabilities is not None:
        entry["capabilities"] = capabilities
    if input_modalities is not None:
        entry["input_modalities"] = input_modalities
    return entry


def _model_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        models = payload.get("data")
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, dict)]


def _is_lmstudio_llm(model: dict[str, Any]) -> bool:
    kind = _first_string(model, "type", "model_type", "compatibility_type")
    if kind is None:
        return False
    normalized = kind.lower()
    if any(token in normalized for token in ("embedding", "rerank", "tts")):
        return False
    return (
        "llm" in normalized
        or "language" in normalized
        or "vlm" in normalized
        or normalized in {"gguf", "mlx"}
    )


def _lmstudio_advertised_modalities(model: dict[str, Any]) -> list[str] | None:
    kind = _first_string(model, "type", "model_type", "compatibility_type")
    if kind is None:
        return None
    normalized = kind.lower()
    if "vlm" in normalized:
        return ["text", "image"]
    if "llm" in normalized or "language" in normalized:
        return ["text"]
    return None


def _ollama_advertised_modalities(model: dict[str, Any]) -> list[str] | None:
    capabilities = model.get("capabilities")
    if not isinstance(capabilities, list):
        return None
    tokens = {
        capability.lower()
        for capability in capabilities
        if isinstance(capability, str) and capability.strip()
    }
    if "vision" in tokens:
        return ["text", "image"]
    if "completion" in tokens:
        return ["text"]
    return None


def _entry_input_modalities(
    endpoint: GenerationEndpointConfig,
    model_id: str,
    advertised: list[str] | None,
) -> list[str] | None:
    if endpoint.probed_model == model_id and endpoint.input_modalities is not None:
        return list(endpoint.input_modalities)
    return advertised


def _ollama_label(model: dict[str, Any], model_id: str) -> str:
    return _first_string(model, "name", "model") or model_id


def _context_length(model: dict[str, Any]) -> int | None:
    for key in (
        "context_length",
        "max_context_length",
        "max_model_len",
        "context_window",
        "max_context_window",
        "ctx_length",
        "num_ctx",
    ):
        value = model.get(key)
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _capabilities(model: dict[str, Any]) -> Any | None:
    value = model.get("capabilities")
    return value if isinstance(value, list | dict) else None


def _ollama_capabilities(model: dict[str, Any]) -> dict[str, Any] | None:
    details = model.get("details")
    if not isinstance(details, dict):
        return None
    result = {
        key: details[key]
        for key in ("family", "families", "parameter_size", "quantization_level")
        if key in details
    }
    return result or None


def _first_string(model: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = model.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _origin(api_base: str) -> str:
    base = api_base.rstrip("/")
    if base.endswith("/v1"):
        return base[:-3]
    return base


def _headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _short_error(exc: BaseException) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


async def probe_generation_endpoint(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    *,
    timeout: float = 1.5,
) -> dict[str, Any]:
    """Probe one configured generation endpoint's ``/v1/models`` for live health."""
    result: dict[str, Any] = {
        "name": endpoint_name,
        "protocol": endpoint.protocol,
        "provider_label": local_provider_display_label(endpoint.protocol),
        "wire_api": endpoint.wire_api,
        "api_base": endpoint.api_base,
        "model": endpoint.model,
        "healthy": False,
        "served_model": None,
        "model_count": None,
        "error": None,
    }
    models_url = vllm_models_url(endpoint.api_base)
    try:
        async with httpx.AsyncClient() as client:
            response = await asyncio.wait_for(
                client.get(models_url, headers=_headers(endpoint.api_key), timeout=timeout),
                timeout,
            )
        response.raise_for_status()
        payload: Any = response.json()
    except Exception as exc:
        result["error"] = _short_error(exc)
        return result

    served = [
        model_id
        for model in _model_list(payload)
        if (model_id := _first_string(model, "id", "model", "name")) is not None
    ]
    result["model_count"] = len(served)
    if endpoint.protocol == "vllm":
        try:
            result["served_model"] = select_vllm_served_model(
                endpoint.model, served, api_base=endpoint.api_base
            )
        except LocalModelError as exc:
            result["error"] = str(exc)
            return result
    else:
        result["served_model"] = endpoint.model
    result["healthy"] = True
    return result


async def probe_generation_endpoints(
    endpoints: dict[str, GenerationEndpointConfig],
    *,
    timeout: float = 1.5,
) -> list[dict[str, Any]]:
    """Probe every configured generation endpoint concurrently; never raises."""
    if not endpoints:
        return []

    async def _probe(name: str, endpoint: GenerationEndpointConfig) -> dict[str, Any]:
        try:
            return await probe_generation_endpoint(name, endpoint, timeout=timeout)
        except Exception as exc:
            return {
                "name": name,
                "protocol": getattr(endpoint, "protocol", None),
                "provider_label": local_provider_display_label(
                    str(getattr(endpoint, "protocol", "") or "")
                ),
                "wire_api": getattr(endpoint, "wire_api", None),
                "api_base": getattr(endpoint, "api_base", None),
                "model": getattr(endpoint, "model", None),
                "healthy": False,
                "served_model": None,
                "model_count": None,
                "error": _short_error(exc),
            }

    return list(
        await asyncio.gather(*(_probe(name, endpoint) for name, endpoint in endpoints.items()))
    )
