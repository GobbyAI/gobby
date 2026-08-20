"""Invocation-scoped Codex configuration for Responses and vLLM chat endpoints."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from gobby.config.ai import GenerationEndpointConfig
from gobby.paths import get_gobby_home

CODEX_ENDPOINT_API_KEY_ENV = "GOBBY_CODEX_ENDPOINT_API_KEY"
_CODEX_ENDPOINT_HOME_DIR = "codex-endpoints"
_VLLM_AUTO_MODEL = "auto"


def codex_endpoint_provider_id(endpoint_name: str) -> str:
    """Return the stable Codex provider ID for a named generation endpoint."""
    return f"gobby_endpoint_{endpoint_name}"


def codex_vllm_provider_id(endpoint_name: str) -> str:
    """Return the namespaced Codex provider ID for a vLLM generation endpoint."""
    return f"gobby-vllm-{endpoint_name}"


def codex_endpoint_display_name(endpoint_name: str) -> str:
    if endpoint_name == "openrouter":
        return "OpenRouter"
    return endpoint_name.replace("-", " ").replace("_", " ").title()


def _is_vllm_auto_model(model: str) -> bool:
    return model.strip() == _VLLM_AUTO_MODEL


def _vllm_chat_config_overrides(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    *,
    model: str | None,
) -> tuple[str, ...]:
    provider_id = codex_vllm_provider_id(endpoint_name)
    selected_model = model or endpoint.model
    quote = json.dumps
    lines = [
        f"model_provider={quote(provider_id)}",
    ]
    if not _is_vllm_auto_model(selected_model):
        lines.append(f"model={quote(selected_model)}")
    lines.extend(
        [
            f"model_providers.{provider_id}.name={quote(f'vLLM ({endpoint_name})')}",
            f"model_providers.{provider_id}.base_url={quote(endpoint.api_base)}",
        ]
    )
    if endpoint.api_key:
        lines.append(f"model_providers.{provider_id}.env_key={quote(CODEX_ENDPOINT_API_KEY_ENV)}")
    lines.append(f"model_providers.{provider_id}.wire_api={quote('chat')}")
    if endpoint.api_key:
        lines.append(f"shell_environment_policy.exclude={quote([CODEX_ENDPOINT_API_KEY_ENV])}")
    lines.append("features.shell_snapshot=false")
    return tuple(lines)


def codex_endpoint_config_overrides(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    *,
    model: str | None = None,
) -> tuple[str, ...]:
    """Build secret-free Codex config overrides for Responses or vLLM chat."""
    if endpoint.protocol == "vllm":
        return _vllm_chat_config_overrides(endpoint_name, endpoint, model=model)
    if endpoint.wire_api != "responses":
        raise ValueError("Codex endpoint overrides require wire_api='responses'")
    provider_id = codex_endpoint_provider_id(endpoint_name)
    selected_model = model or endpoint.model
    quote = json.dumps
    return (
        f"model_provider={quote(provider_id)}",
        f"model={quote(selected_model)}",
        f"model_providers.{provider_id}.name={quote(codex_endpoint_display_name(endpoint_name))}",
        f"model_providers.{provider_id}.base_url={quote(endpoint.api_base)}",
        f"model_providers.{provider_id}.env_key={quote(CODEX_ENDPOINT_API_KEY_ENV)}",
        f"model_providers.{provider_id}.wire_api={quote('responses')}",
        f"shell_environment_policy.exclude={quote([CODEX_ENDPOINT_API_KEY_ENV])}",
        "features.shell_snapshot=false",
    )


def codex_model_config_override(model: str) -> str:
    return f"model={json.dumps(model)}"


def codex_event_text(event: Mapping[str, Any]) -> str:
    """Extract assistant text from a Codex app-server event."""
    delta = event.get("delta")
    if isinstance(delta, str) and delta:
        return delta
    item = event.get("item")
    if not isinstance(item, dict):
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for block in content:
        if isinstance(block, dict):
            text = block.get("text") or block.get("delta")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "".join(chunks)


def codex_endpoint_env(endpoint: GenerationEndpointConfig) -> Mapping[str, str]:
    """Return the child-only API key environment for a Responses endpoint."""
    api_key = endpoint.api_key
    if not api_key or api_key.startswith("$secret:"):
        raise ValueError(
            "Generation endpoint API key is unavailable; configure its referenced secret"
        )
    return {CODEX_ENDPOINT_API_KEY_ENV: api_key}


def codex_endpoint_app_server_env(
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
) -> Mapping[str, str]:
    """Return an isolated child environment for a Codex endpoint app-server."""
    codex_home = get_gobby_home() / _CODEX_ENDPOINT_HOME_DIR / endpoint_name
    codex_home.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {"CODEX_HOME": str(codex_home)}
    if endpoint.protocol == "vllm" and not endpoint.api_key:
        return env
    env.update(codex_endpoint_env(endpoint))
    return env
