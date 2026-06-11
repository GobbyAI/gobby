"""Helpers for named local generation endpoint selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gobby.config.ai import LocalGenerationEndpointConfig

LOCAL_ENDPOINT_PROVIDER_PREFIX = "local:"


@dataclass(frozen=True)
class LocalGenerationEndpointSelection:
    """Resolved named local generation endpoint."""

    name: str
    endpoint: LocalGenerationEndpointConfig

    @property
    def provider(self) -> str:
        return local_endpoint_provider(self.name)


def local_endpoint_provider(name: str) -> str:
    """Return the provider label for a named local generation endpoint."""
    return f"{LOCAL_ENDPOINT_PROVIDER_PREFIX}{name}"


def parse_local_endpoint_selector(value: str | None) -> str | None:
    """Return endpoint name from a local:<endpoint> selector, if present."""
    if not isinstance(value, str):
        return None
    selector = value.strip()
    if not selector.startswith(LOCAL_ENDPOINT_PROVIDER_PREFIX):
        return None
    endpoint_name = selector.removeprefix(LOCAL_ENDPOINT_PROVIDER_PREFIX).strip()
    if not endpoint_name or "/" in endpoint_name:
        raise ValueError(f"local endpoint selectors must use local:<endpoint>; got {value!r}")
    return endpoint_name


def resolve_local_generation_endpoint(
    config: Any,
    endpoint_name: str,
) -> LocalGenerationEndpointConfig:
    """Resolve a named local generation endpoint from daemon config."""
    endpoints = _local_generation_endpoints(config)
    try:
        return endpoints[endpoint_name]
    except KeyError as exc:
        raise ValueError(f"Unknown local generation endpoint: {endpoint_name}") from exc


def resolve_local_generation_endpoint_selector(
    config: Any,
    selector: str | None,
) -> LocalGenerationEndpointSelection | None:
    """Resolve local:<endpoint> into endpoint config, or None for non-local values."""
    endpoint_name = parse_local_endpoint_selector(selector)
    if endpoint_name is None:
        return None
    return LocalGenerationEndpointSelection(
        name=endpoint_name,
        endpoint=resolve_local_generation_endpoint(config, endpoint_name),
    )


def _local_generation_endpoints(config: Any) -> dict[str, LocalGenerationEndpointConfig]:
    ai_cfg = getattr(config, "ai", None)
    generation_cfg = getattr(ai_cfg, "generation", None)
    local_cfg = getattr(generation_cfg, "local", None)
    endpoints = getattr(local_cfg, "endpoints", None)
    if not isinstance(endpoints, dict):
        return {}
    return endpoints
