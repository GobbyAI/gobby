"""Generation endpoint selector parsing and resolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, overload

from gobby.config.ai import GenerationEndpointConfig

ENDPOINT_PROVIDER_PREFIX = "endpoint:"
REMOVED_LOCAL_PROVIDER_PREFIX = "local:"


@dataclass(frozen=True)
class GenerationEndpointSelection:
    """Resolved named generation endpoint."""

    name: str
    endpoint: GenerationEndpointConfig
    model: str | None = None

    @property
    def provider(self) -> str:
        return endpoint_provider(self.name)

    @property
    def selected_model(self) -> str:
        return self.model or self.endpoint.model

    def endpoint_with_selected_model(self) -> GenerationEndpointConfig:
        if self.model is None:
            return self.endpoint
        return self.endpoint.model_copy(update={"model": self.model})


@dataclass(frozen=True)
class GenerationEndpointSelector:
    endpoint_name: str
    model: str | None = None


def endpoint_provider(name: str) -> str:
    return f"{ENDPOINT_PROVIDER_PREFIX}{name}"


@overload
def normalize_endpoint_routing(provider: str, model: str) -> tuple[str, str]: ...


@overload
def normalize_endpoint_routing(
    provider: str | None,
    model: str | None,
) -> tuple[str | None, str | None]: ...


def normalize_endpoint_routing(
    provider: str | None,
    model: str | None,
) -> tuple[str | None, str | None]:
    """Map a Codex-facing endpoint selector to its logical capability binding."""
    selector = parse_endpoint_model_selector(model)
    if selector is None or provider is None:
        return provider, model

    logical_provider = endpoint_provider(selector.endpoint_name)
    if provider not in {"codex", logical_provider}:
        return provider, model
    return logical_provider, selector.model


def _reject_removed_local_selector(value: str) -> None:
    if value.strip().startswith(REMOVED_LOCAL_PROVIDER_PREFIX):
        raise ValueError(f"{value!r} uses the removed local: selector; replace it with endpoint:*")


def parse_endpoint_selector(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    _reject_removed_local_selector(value)
    selector = parse_endpoint_model_selector(value)
    return selector.endpoint_name if selector is not None else None


def parse_endpoint_model_selector(value: str | None) -> GenerationEndpointSelector | None:
    """Parse endpoint:<name>[/<model-id>] while preserving model slashes."""
    if not isinstance(value, str):
        return None
    _reject_removed_local_selector(value)
    selector = value.strip()
    if not selector.startswith(ENDPOINT_PROVIDER_PREFIX):
        return None
    body = selector.removeprefix(ENDPOINT_PROVIDER_PREFIX).strip()
    endpoint_name, separator, model = body.partition("/")
    endpoint_name = endpoint_name.strip()
    model = model.strip()
    if not endpoint_name or (separator and not model):
        raise ValueError(
            "generation endpoint selectors must use endpoint:<name> or "
            f"endpoint:<name>/<model-id>; got {value!r}"
        )
    return GenerationEndpointSelector(
        endpoint_name=endpoint_name,
        model=model if separator else None,
    )


def resolve_generation_endpoint(
    config: Any,
    endpoint_name: str,
) -> GenerationEndpointConfig:
    """Resolve a named generation endpoint from daemon config."""
    endpoints = _generation_endpoints(config)
    try:
        return endpoints[endpoint_name]
    except KeyError as exc:
        raise ValueError(f"Unknown generation endpoint: {endpoint_name}") from exc


def resolve_generation_endpoint_selector(
    config: Any,
    value: str | None,
) -> GenerationEndpointSelection | None:
    selector = parse_endpoint_model_selector(value)
    if selector is None:
        return None
    return GenerationEndpointSelection(
        name=selector.endpoint_name,
        endpoint=resolve_generation_endpoint(config, selector.endpoint_name),
        model=selector.model,
    )


def _generation_endpoints(config: Any) -> dict[str, GenerationEndpointConfig]:
    try:
        endpoints = config.ai.generation.endpoints
    except AttributeError as exc:
        raise ValueError("Generation endpoint configuration is unavailable") from exc
    if not isinstance(endpoints, dict):
        raise ValueError("Generation endpoint configuration is invalid")
    return endpoints
