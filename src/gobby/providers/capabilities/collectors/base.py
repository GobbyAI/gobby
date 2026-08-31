"""Provider capability collector contracts, registry, and snapshot validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from gobby.providers.capabilities.activation import (
    ActivationValidationError,
    validate_activation,
)
from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    SourceState,
    SpeedMode,
)


class SnapshotValidationError(ValueError):
    """Raised when a collector snapshot is unsafe to persist."""


@dataclass(frozen=True)
class SourceSpec:
    """One source used to build a provider capability snapshot."""

    source_key: str
    url: str | None
    required: bool
    fast_only_selectors: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fast_only_selectors", frozenset(self.fast_only_selectors))


class CapabilityCollector(Protocol):
    """Collector contract implemented by each capability provider adapter."""

    provider: str
    sources: tuple[SourceSpec, ...]

    async def collect(self) -> ProviderSnapshot:
        """Collect one complete provider snapshot."""
        ...


_COLLECTORS: dict[str, CapabilityCollector] = {}

_MODEL_FACTS = frozenset(
    {
        "canonical_model",
        "display_name",
        "aliases",
        "available",
        "hidden",
        "is_default",
        "reasoning",
    }
)
_OPTIONAL_MODEL_FACTS = (
    "context_length",
    "max_output_tokens",
    "supported_efforts",
    "default_effort",
    "latency_class",
    "input_modalities",
    "supports_tools",
)
_ROUTE_FACTS = frozenset({"speed_mode", "selector", "available", "activations"})
_OPTIONAL_ROUTE_FACTS = (
    "usage_multiplier",
    "throughput_multiplier",
    "latency_class",
)


def register_collector(collector: CapabilityCollector) -> None:
    """Register a collector under its provider key."""
    provider = _normalized(collector.provider, "collector provider")
    _source_specs(collector.sources)
    if provider in _COLLECTORS:
        raise SnapshotValidationError(f"Collector provider {provider!r} is already registered")
    _COLLECTORS[provider] = collector


def collectors() -> Mapping[str, CapabilityCollector]:
    """Return an immutable provider-keyed view of registered collectors."""
    return MappingProxyType(dict(_COLLECTORS))


def validate_snapshot(
    snapshot: ProviderSnapshot,
    sources: tuple[SourceSpec, ...],
) -> ProviderSnapshot:
    """Validate a successful collector snapshot before any store write."""
    _normalized(snapshot.provider, "snapshot provider")
    source_specs = _source_specs(sources)
    if not snapshot.models:
        raise SnapshotValidationError("Successful snapshots require at least one model")

    _validate_source_health(snapshot, source_specs)
    seen_models: set[str] = set()
    fast_only_selectors = frozenset().union(
        *(spec.fast_only_selectors for spec in source_specs.values())
    )
    for model in snapshot.models:
        model_name = _normalized(model.canonical_model, "canonical model")
        if model_name in seen_models:
            raise SnapshotValidationError(f"Duplicate canonical model: {model_name!r}")
        seen_models.add(model_name)
        _validate_model(model, source_specs, fast_only_selectors)
    return snapshot


def _source_specs(sources: tuple[SourceSpec, ...]) -> dict[str, SourceSpec]:
    if not sources:
        raise SnapshotValidationError("Collectors require at least one source")
    result: dict[str, SourceSpec] = {}
    for source in sources:
        source_key = _normalized(source.source_key, "source key")
        if source_key in result:
            raise SnapshotValidationError(f"Duplicate source key: {source_key!r}")
        for selector in source.fast_only_selectors:
            _normalized(selector, "fast-only selector")
        result[source_key] = source
    return result


def _validate_source_health(
    snapshot: ProviderSnapshot,
    source_specs: Mapping[str, SourceSpec],
) -> None:
    seen_health: set[str] = set()
    for health in snapshot.sources:
        source_key = _normalized(health.source_key, "source health key")
        if source_key in seen_health:
            raise SnapshotValidationError(f"Duplicate source health: {source_key!r}")
        seen_health.add(source_key)
        spec = source_specs.get(source_key)
        if spec is None:
            raise SnapshotValidationError(f"Undeclared snapshot source: {source_key!r}")
        if health.source_url != spec.url or health.required != spec.required:
            raise SnapshotValidationError(
                f"Source health does not match declaration: {source_key!r}"
            )
        if spec.required and health.state is not SourceState.OK:
            raise SnapshotValidationError(f"Required source is not healthy: {source_key!r}")

    missing = source_specs.keys() - seen_health
    if missing:
        raise SnapshotValidationError(
            f"Snapshot is missing source health: {', '.join(sorted(missing))}"
        )


def _validate_model(
    model: ModelCapability,
    source_specs: Mapping[str, SourceSpec],
    fast_only_selectors: frozenset[str],
) -> None:
    _normalized(model.display_name, f"display name for {model.canonical_model!r}")
    if not model.routes:
        raise SnapshotValidationError(f"Model {model.canonical_model!r} requires a route")

    model_facts = set(_MODEL_FACTS)
    model_facts.update(fact for fact in _OPTIONAL_MODEL_FACTS if getattr(model, fact) is not None)
    _validate_provenance(
        model.provenance,
        frozenset(model_facts),
        source_specs,
        f"model {model.canonical_model!r}",
    )

    routes_by_mode: dict[SpeedMode, ModelRoute] = {}
    for route in model.routes:
        if route.speed_mode in routes_by_mode:
            raise SnapshotValidationError(
                f"Model {model.canonical_model!r} has duplicate {route.speed_mode.value!r} routes"
            )
        routes_by_mode[route.speed_mode] = route
        _validate_route(route, model.canonical_model, source_specs)

    fast_route = routes_by_mode.get(SpeedMode.FAST)
    if (
        fast_route is not None
        and SpeedMode.STANDARD not in routes_by_mode
        and fast_route.selector not in fast_only_selectors
    ):
        raise SnapshotValidationError(
            f"Fast route {fast_route.selector!r} requires a standard route or source declaration"
        )


def _validate_route(
    route: ModelRoute,
    model_name: str,
    source_specs: Mapping[str, SourceSpec],
) -> None:
    _normalized(route.selector, f"route selector for {model_name!r}")
    route_facts = set(_ROUTE_FACTS)
    route_facts.update(fact for fact in _OPTIONAL_ROUTE_FACTS if getattr(route, fact) is not None)
    _validate_provenance(
        route.provenance,
        frozenset(route_facts),
        source_specs,
        f"route {route.selector!r}",
    )
    for activation in route.activations:
        try:
            validate_activation(activation)
        except ActivationValidationError as error:
            raise SnapshotValidationError(
                f"Invalid activation on route {route.selector!r}: {error}"
            ) from error


def _validate_provenance(
    provenance: Mapping[str, FactProvenance],
    required_facts: frozenset[str],
    source_specs: Mapping[str, SourceSpec],
    owner: str,
) -> None:
    missing = required_facts - provenance.keys()
    if missing:
        raise SnapshotValidationError(
            f"{owner} is missing provenance for facts: {', '.join(sorted(missing))}"
        )
    for fact_name, fact_provenance in provenance.items():
        _normalized(fact_name, "provenance fact name")
        if fact_provenance.source_key == "bundled":
            if fact_provenance.source_url is not None:
                raise SnapshotValidationError(
                    f"{owner} bundled provenance must not declare a source URL"
                )
            if fact_provenance.observed_at.utcoffset() is None:
                raise SnapshotValidationError(
                    f"{owner} provenance timestamps must be timezone-aware"
                )
            continue
        spec = source_specs.get(fact_provenance.source_key)
        if spec is None:
            raise SnapshotValidationError(
                f"{owner} provenance uses undeclared source {fact_provenance.source_key!r}"
            )
        if fact_provenance.source_url != spec.url:
            raise SnapshotValidationError(
                f"{owner} provenance URL does not match source {spec.source_key!r}"
            )
        if fact_provenance.observed_at.utcoffset() is None:
            raise SnapshotValidationError(f"{owner} provenance timestamps must be timezone-aware")


def _normalized(value: str, field: str) -> str:
    if not value or value.strip() != value:
        raise SnapshotValidationError(f"{field} must be a non-empty normalized string")
    return value
