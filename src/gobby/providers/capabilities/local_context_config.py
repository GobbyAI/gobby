"""Normalize configured generation and CLI endpoints into local-context routes."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from gobby.ai.endpoints import parse_endpoint_model_selector
from gobby.config.ai import GenerationEndpointConfig, GenerationEndpointProtocol
from gobby.config.app import DaemonConfig, deep_merge
from gobby.config.feature_base import iter_feature_default_configs, parse_feature_candidate
from gobby.providers.capabilities.local_context import (
    LocalContextIdentity,
    LocalContextObservation,
)
from gobby.servers.provider_model_discovery import (
    is_loopback_model_endpoint,
    load_claude_settings,
    load_codex_config,
    load_qwen_settings,
)

logger = logging.getLogger(__name__)

_CLI_PROVIDERS = frozenset({"claude", "codex", "droid", "grok", "qwen"})
_NATIVE_PATH_SUFFIXES: Mapping[GenerationEndpointProtocol, tuple[str, ...]] = {
    "openai-compatible": ("/v1",),
    "lmstudio": ("/api/v1", "/v1"),
    "ollama": ("/api/v1", "/v1", "/api"),
    "vllm": ("/v1",),
}


@dataclass(frozen=True, slots=True)
class LocalContextRoute:
    """One exact model selection at a configured or CLI endpoint."""

    machine_id: str
    endpoint_id: str
    configuration_fingerprint: str
    provider: str
    protocol: GenerationEndpointProtocol
    model_id: str
    api_base: str = field(repr=False, compare=False)
    api_key: str | None = field(default=None, repr=False, compare=False)
    instance_id: str | None = None
    is_local: bool = False

    def __post_init__(self) -> None:
        for name in (
            "machine_id",
            "endpoint_id",
            "configuration_fingerprint",
            "provider",
            "model_id",
            "api_base",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.instance_id is not None and (
            not isinstance(self.instance_id, str) or not self.instance_id.strip()
        ):
            raise ValueError("instance_id must be a non-empty string when set")

    @property
    def identity(self) -> LocalContextIdentity:
        return LocalContextIdentity(
            machine_id=self.machine_id,
            endpoint_id=self.endpoint_id,
            configuration_fingerprint=self.configuration_fingerprint,
            api_base=self.api_base,
            api_key=self.api_key,
        )

    @property
    def refresh_key(self) -> tuple[str, str, str, str, str, str | None, bool]:
        """Identity used to coalesce only equivalent refresh work."""
        return (
            self.machine_id,
            self.endpoint_id,
            self.configuration_fingerprint,
            self.protocol,
            self.model_id,
            self.instance_id,
            self.is_local,
        )

    def matches_observation(self, observation: LocalContextObservation) -> bool:
        """Require exact route identity before using persisted evidence."""
        return (
            observation.machine_id == self.machine_id
            and observation.endpoint_id == self.endpoint_id
            and observation.configuration_fingerprint == self.configuration_fingerprint
            and observation.model_id == self.model_id
            and observation.instance_id == self.instance_id
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a credential-free route projection."""
        return {
            "machine_id": self.machine_id,
            "endpoint_id": self.endpoint_id,
            "configuration_fingerprint": self.configuration_fingerprint,
            "provider": self.provider,
            "protocol": self.protocol,
            "model_id": self.model_id,
            "instance_id": self.instance_id,
            "api_base": self.api_base,
            "is_local": self.is_local,
        }


def endpoint_route(
    *,
    machine_id: str,
    endpoint_name: str,
    endpoint: GenerationEndpointConfig,
    provider: str | None = None,
    model_id: str | None = None,
    instance_id: str | None = None,
) -> LocalContextRoute:
    """Build an exact route for a named generation endpoint."""
    api_base = _normalized_api_base(endpoint.api_base)
    selected_model = _required_text(model_id or endpoint.model, "model_id")
    return LocalContextRoute(
        machine_id=_required_text(machine_id, "machine_id"),
        endpoint_id=f"endpoint:{_required_text(endpoint_name, 'endpoint_name')}",
        configuration_fingerprint=_configuration_fingerprint(
            protocol=endpoint.protocol,
            wire_api=endpoint.wire_api,
            api_base=api_base,
            api_key=endpoint.api_key,
        ),
        provider=provider or f"endpoint:{endpoint_name}",
        protocol=endpoint.protocol,
        model_id=selected_model,
        instance_id=instance_id,
        api_base=api_base,
        api_key=endpoint.api_key,
        is_local=is_loopback_model_endpoint(api_base),
    )


def cli_endpoint_route(
    *,
    machine_id: str,
    provider: str,
    model_id: str,
    api_base: str,
    api_key: str | None,
    endpoints: Mapping[str, GenerationEndpointConfig],
    instance_id: str | None = None,
    endpoint_scope: str = "active",
) -> LocalContextRoute:
    """Match a CLI route to configured endpoint identity or use generic discovery."""
    normalized_provider = _required_text(provider, "provider").lower()
    normalized_base = _normalized_api_base(api_base)
    match = _matching_endpoint(normalized_base, api_key, endpoints)
    if match is not None:
        endpoint_name, endpoint = match
        return endpoint_route(
            machine_id=machine_id,
            endpoint_name=endpoint_name,
            endpoint=endpoint,
            provider=normalized_provider,
            model_id=model_id,
            instance_id=instance_id,
        )

    scope = _required_text(endpoint_scope, "endpoint_scope")
    return LocalContextRoute(
        machine_id=_required_text(machine_id, "machine_id"),
        endpoint_id=f"cli:{normalized_provider}:{scope}",
        configuration_fingerprint=_configuration_fingerprint(
            protocol="openai-compatible",
            wire_api="chat-completions",
            api_base=normalized_base,
            api_key=api_key,
        ),
        provider=normalized_provider,
        protocol="openai-compatible",
        model_id=_required_text(model_id, "model_id"),
        instance_id=instance_id,
        api_base=normalized_base,
        api_key=api_key,
        is_local=is_loopback_model_endpoint(normalized_base),
    )


def configured_local_routes(
    config: DaemonConfig,
    *,
    machine_id: str,
    environment: Mapping[str, str] | None = None,
    codex_config: Mapping[str, Any] | None = None,
    claude_settings: Mapping[str, Any] | None = None,
    qwen_settings: Mapping[str, Any] | None = None,
) -> tuple[LocalContextRoute, ...]:
    """Read current endpoint and CLI settings and return exact local routes."""
    endpoints = config.ai.generation.endpoints
    env = os.environ if environment is None else environment
    routes: list[LocalContextRoute] = [
        endpoint_route(machine_id=machine_id, endpoint_name=name, endpoint=endpoint)
        for name, endpoint in endpoints.items()
    ]
    routes.extend(_configured_cli_endpoint_routes(config, machine_id, endpoints))

    loaded_codex = load_codex_config(logger=logger) if codex_config is None else dict(codex_config)
    codex_route = _codex_route(machine_id, endpoints, loaded_codex, env)
    if codex_route is not None:
        routes.append(codex_route)

    loaded_claude = (
        load_claude_settings(deep_merge=deep_merge, logger=logger)
        if claude_settings is None
        else dict(claude_settings)
    )
    claude_route = _claude_route(machine_id, endpoints, loaded_claude, env)
    if claude_route is not None:
        routes.append(claude_route)

    loaded_qwen = (
        load_qwen_settings(deep_merge=deep_merge, logger=logger)
        if qwen_settings is None
        else dict(qwen_settings)
    )
    qwen_route = _qwen_route(machine_id, endpoints, loaded_qwen, env)
    if qwen_route is not None:
        routes.append(qwen_route)

    unique = {route: None for route in routes if route.is_local}
    return tuple(unique)


def _configured_cli_endpoint_routes(
    config: DaemonConfig,
    machine_id: str,
    endpoints: Mapping[str, GenerationEndpointConfig],
) -> Iterable[LocalContextRoute]:
    for feature in iter_feature_default_configs(config):
        for candidate in feature.candidates:
            provider, model = parse_feature_candidate(candidate)
            if provider not in _CLI_PROVIDERS:
                continue
            selector = parse_endpoint_model_selector(model)
            if selector is None:
                continue
            endpoint = endpoints.get(selector.endpoint_name)
            if endpoint is None:
                continue
            yield endpoint_route(
                machine_id=machine_id,
                endpoint_name=selector.endpoint_name,
                endpoint=endpoint,
                provider=provider,
                model_id=selector.model or endpoint.model,
            )


def _codex_route(
    machine_id: str,
    endpoints: Mapping[str, GenerationEndpointConfig],
    config: Mapping[str, Any],
    environment: Mapping[str, str],
) -> LocalContextRoute | None:
    provider_id = config.get("model_provider")
    providers = config.get("model_providers")
    model_id = _optional_text(config.get("model")) or _optional_text(
        environment.get("OPENAI_MODEL")
    )
    if not isinstance(provider_id, str) or not isinstance(providers, Mapping) or model_id is None:
        return None
    provider_config = providers.get(provider_id)
    if not isinstance(provider_config, Mapping):
        return None
    api_base = _optional_text(provider_config.get("base_url"))
    if api_base is None:
        return None
    env_key = _optional_text(provider_config.get("env_key"))
    api_key = _optional_text(environment.get(env_key)) if env_key is not None else None
    return _safe_cli_route(
        machine_id=machine_id,
        provider="codex",
        model_id=model_id,
        api_base=api_base,
        api_key=api_key,
        endpoints=endpoints,
        endpoint_scope=provider_id,
    )


def _claude_route(
    machine_id: str,
    endpoints: Mapping[str, GenerationEndpointConfig],
    settings: Mapping[str, Any],
    environment: Mapping[str, str],
) -> LocalContextRoute | None:
    configured_env = settings.get("env")
    settings_env = configured_env if isinstance(configured_env, Mapping) else {}
    api_base = _effective_env("ANTHROPIC_BASE_URL", environment, settings_env)
    model_id = (
        _effective_env("ANTHROPIC_MODEL", environment, settings_env)
        or _optional_text(settings.get("model"))
        or _effective_env("ANTHROPIC_DEFAULT_MODEL", environment, settings_env)
    )
    if api_base is None or model_id is None:
        return None
    api_key = _effective_env("ANTHROPIC_API_KEY", environment, settings_env) or _effective_env(
        "ANTHROPIC_AUTH_TOKEN", environment, settings_env
    )
    return _safe_cli_route(
        machine_id=machine_id,
        provider="claude",
        model_id=model_id,
        api_base=api_base,
        api_key=api_key,
        endpoints=endpoints,
    )


def _qwen_route(
    machine_id: str,
    endpoints: Mapping[str, GenerationEndpointConfig],
    settings: Mapping[str, Any],
    environment: Mapping[str, str],
) -> LocalContextRoute | None:
    selected = settings.get("model")
    providers = settings.get("modelProviders")
    if not isinstance(selected, Mapping) or not isinstance(providers, Mapping):
        return None
    model_id = _optional_text(selected.get("name"))
    openai_entries = providers.get("openai")
    if model_id is None or not isinstance(openai_entries, list):
        return None
    entry = next(
        (
            value
            for value in openai_entries
            if isinstance(value, Mapping) and value.get("id") == model_id
        ),
        None,
    )
    if entry is None:
        return None
    api_base = _optional_text(entry.get("baseUrl"))
    if api_base is None:
        return None
    env_key = _optional_text(entry.get("envKey"))
    api_key = _optional_text(entry.get("apiKey"))
    if api_key is None and env_key is not None:
        api_key = _optional_text(environment.get(env_key))
    if api_key is None:
        api_key = _first_env(environment, "QWEN_API_KEY", "OPENAI_API_KEY")
    return _safe_cli_route(
        machine_id=machine_id,
        provider="qwen",
        model_id=model_id,
        api_base=api_base,
        api_key=api_key,
        endpoints=endpoints,
        endpoint_scope=f"openai:{model_id}",
    )


def _safe_cli_route(
    *,
    machine_id: str,
    provider: str,
    model_id: str,
    api_base: str,
    api_key: str | None,
    endpoints: Mapping[str, GenerationEndpointConfig],
    instance_id: str | None = None,
    endpoint_scope: str = "active",
) -> LocalContextRoute | None:
    try:
        return cli_endpoint_route(
            machine_id=machine_id,
            provider=provider,
            model_id=model_id,
            api_base=api_base,
            api_key=api_key,
            endpoints=endpoints,
            instance_id=instance_id,
            endpoint_scope=endpoint_scope,
        )
    except (TypeError, ValueError):
        return None


def _matching_endpoint(
    api_base: str,
    api_key: str | None,
    endpoints: Mapping[str, GenerationEndpointConfig],
) -> tuple[str, GenerationEndpointConfig] | None:
    candidates = [
        (name, endpoint)
        for name, endpoint in endpoints.items()
        if _safe_endpoint_identity_base(endpoint.api_base, endpoint.protocol)
        == _endpoint_identity_base(api_base, endpoint.protocol)
    ]
    if api_key is not None:
        candidates = [item for item in candidates if item[1].api_key == api_key]
    elif len(candidates) > 1:
        credential_free = [item for item in candidates if item[1].api_key is None]
        candidates = credential_free if len(credential_free) == 1 else []
    return candidates[0] if len(candidates) == 1 else None


def _configuration_fingerprint(
    *,
    protocol: GenerationEndpointProtocol,
    wire_api: str,
    api_base: str,
    api_key: str | None,
) -> str:
    credential = hashlib.sha256(api_key.encode()).hexdigest() if api_key else None
    payload = json.dumps(
        {
            "protocol": protocol,
            "wire_api": wire_api,
            "api_base": _endpoint_identity_base(api_base, protocol),
            "credential": credential,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _normalized_api_base(value: str) -> str:
    url = httpx.URL(_required_text(value, "api_base"))
    if url.scheme not in {"http", "https"} or url.host is None:
        raise ValueError("api_base must be an HTTP(S) URL with a host")
    path = url.path.rstrip("/")
    clean = url.copy_with(
        path=path or "/",
        username=None,
        password=None,
        query=None,
        fragment=None,
    )
    return str(clean).rstrip("/")


def _endpoint_identity_base(value: str, protocol: GenerationEndpointProtocol) -> str:
    """Return the transport origin after removing one native terminal API path."""
    url = httpx.URL(_normalized_api_base(value))
    path = url.path.rstrip("/")
    for suffix in _NATIVE_PATH_SUFFIXES[protocol]:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return str(url.copy_with(path=path or "/")).rstrip("/")


def _safe_endpoint_identity_base(
    value: str,
    protocol: GenerationEndpointProtocol,
) -> str | None:
    try:
        return _endpoint_identity_base(value, protocol)
    except (TypeError, ValueError):
        return None


def _effective_env(
    key: str,
    environment: Mapping[str, str],
    configured: Mapping[str, Any],
) -> str | None:
    return _optional_text(environment.get(key)) or _optional_text(configured.get(key))


def _first_env(environment: Mapping[str, str], *keys: str) -> str | None:
    return next(
        (value for key in keys if (value := _optional_text(environment.get(key))) is not None),
        None,
    )


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _required_text(value: object, name: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise ValueError(f"{name} must be a non-empty string")
    return normalized
