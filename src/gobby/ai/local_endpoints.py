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
    model: str | None = None

    @property
    def provider(self) -> str:
        return local_endpoint_provider(self.name)

    @property
    def selected_model(self) -> str:
        return self.model or self.endpoint.model

    def endpoint_with_selected_model(self) -> LocalGenerationEndpointConfig:
        if self.model is None:
            return self.endpoint
        return self.endpoint.model_copy(update={"model": self.model})


@dataclass(frozen=True)
class LocalEndpointSelector:
    """Parsed local endpoint selector."""

    endpoint_name: str
    model: str | None = None


def local_endpoint_provider(name: str) -> str:
    """Return the provider label for a named local generation endpoint."""
    return f"{LOCAL_ENDPOINT_PROVIDER_PREFIX}{name}"


def parse_local_endpoint_selector(value: str | None) -> str | None:
    """Return endpoint name from a local:<endpoint> selector, if present."""
    parsed = parse_local_endpoint_model_selector(value)
    return parsed.endpoint_name if parsed is not None else None


def parse_local_endpoint_model_selector(value: str | None) -> LocalEndpointSelector | None:
    """Parse local:<endpoint>[/<model-id>] selectors while preserving model slashes."""
    if not isinstance(value, str):
        return None
    selector = value.strip()
    if not selector.startswith(LOCAL_ENDPOINT_PROVIDER_PREFIX):
        return None
    body = selector.removeprefix(LOCAL_ENDPOINT_PROVIDER_PREFIX).strip()
    endpoint_name, separator, model = body.partition("/")
    endpoint_name = endpoint_name.strip()
    model = model.strip()
    if not endpoint_name or (separator and not model):
        raise ValueError(
            "local endpoint selectors must use local:<endpoint> or "
            f"local:<endpoint>/<model-id>; got {value!r}"
        )
    return LocalEndpointSelector(endpoint_name=endpoint_name, model=model if separator else None)


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
    parsed = parse_local_endpoint_model_selector(selector)
    if parsed is None:
        return None
    return LocalGenerationEndpointSelection(
        name=parsed.endpoint_name,
        endpoint=resolve_local_generation_endpoint(config, parsed.endpoint_name),
        model=parsed.model,
    )


def _local_generation_endpoints(config: Any) -> dict[str, LocalGenerationEndpointConfig]:
    ai_cfg = getattr(config, "ai", None)
    generation_cfg = getattr(ai_cfg, "generation", None)
    local_cfg = getattr(generation_cfg, "local", None)
    endpoints = getattr(local_cfg, "endpoints", None)
    if not isinstance(endpoints, dict):
        return {}
    return endpoints
