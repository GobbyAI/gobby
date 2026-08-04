"""Tests for provider capability collector registration and snapshot validation."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gobby.providers.capabilities.collectors import (
    SnapshotValidationError,
    SourceSpec,
    collectors,
    register_collector,
    validate_snapshot,
)
from gobby.providers.capabilities.models import (
    ActivationDescriptor,
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)

_OBSERVED_AT = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
_SOURCE_URL = "https://example.test/models"
_MODEL_FACTS = frozenset(
    {
        "canonical_model",
        "display_name",
        "aliases",
        "available",
        "hidden",
        "is_default",
        "context_length",
        "max_output_tokens",
        "reasoning",
        "supported_efforts",
        "default_effort",
        "latency_class",
        "input_modalities",
        "supports_tools",
    }
)
_ROUTE_FACTS = frozenset(
    {
        "speed_mode",
        "selector",
        "available",
        "usage_multiplier",
        "throughput_multiplier",
        "latency_class",
        "activations",
    }
)


def _provenance(facts: frozenset[str]) -> dict[str, FactProvenance]:
    provenance = FactProvenance(
        source_key="provider-api",
        source_url=_SOURCE_URL,
        observed_at=_OBSERVED_AT,
    )
    return dict.fromkeys(facts, provenance)


def _source_spec(*, fast_only_selectors: frozenset[str] = frozenset()) -> SourceSpec:
    return SourceSpec(
        source_key="provider-api",
        url=_SOURCE_URL,
        required=True,
        fast_only_selectors=fast_only_selectors,
    )


def _route(
    speed_mode: SpeedMode = SpeedMode.STANDARD,
    *,
    selector: str = "model",
    activations: tuple[ActivationDescriptor, ...] = (),
) -> ModelRoute:
    return ModelRoute(
        speed_mode=speed_mode,
        selector=selector,
        available=True,
        usage_multiplier=Decimal("1"),
        throughput_multiplier=Decimal("2"),
        latency_class="fast",
        activations=activations,
        provenance=_provenance(_ROUTE_FACTS),
    )


def _snapshot(*routes: ModelRoute) -> ProviderSnapshot:
    model = ModelCapability(
        canonical_model="model",
        display_name="Model",
        aliases=("model-latest",),
        available=True,
        hidden=False,
        is_default=True,
        context_length=128_000,
        max_output_tokens=16_000,
        reasoning=ReasoningSupport.KNOWN,
        supported_efforts=("low", "high"),
        default_effort="high",
        latency_class="fast",
        input_modalities=("text",),
        supports_tools=True,
        routes=routes or (_route(),),
        provenance=_provenance(_MODEL_FACTS),
    )
    source = SourceHealth(
        source_key="provider-api",
        source_url=_SOURCE_URL,
        required=True,
        state=SourceState.OK,
        attempts=1,
        last_attempt_at=_OBSERVED_AT,
        last_success_at=_OBSERVED_AT,
        last_error=None,
    )
    return ProviderSnapshot(
        provider="registry-fixture-19624",
        generation=0,
        models=(model,),
        sources=(source,),
    )


class _Collector:
    provider = "registry-fixture-19624"
    sources: tuple[SourceSpec, ...] = (_source_spec(),)

    async def collect(self) -> ProviderSnapshot:
        return _snapshot()


def test_registry_dispatches_by_provider_key() -> None:
    collector = _Collector()

    register_collector(collector)

    assert collectors()[collector.provider] is collector


def test_empty_snapshot_rejected() -> None:
    snapshot = replace(_snapshot(), models=())

    with pytest.raises(SnapshotValidationError, match="at least one model"):
        validate_snapshot(snapshot, (_source_spec(),))


@pytest.mark.parametrize(
    ("route", "message"),
    [
        (_route(selector=" "), "selector"),
        (
            _route(
                activations=(ActivationDescriptor(kind="unknown", surface="spawn-cli", params={}),)
            ),
            "activation",
        ),
        (
            replace(
                _route(),
                provenance={
                    name: value for name, value in _route().provenance.items() if name != "selector"
                },
            ),
            "selector",
        ),
    ],
)
def test_malformed_route_rejected(route: ModelRoute, message: str) -> None:
    snapshot = replace(
        _snapshot(),
        models=(replace(_snapshot().models[0], routes=(route,)),),
    )

    with pytest.raises(SnapshotValidationError, match=message):
        validate_snapshot(snapshot, (_source_spec(),))


def test_model_fact_without_provenance_rejected() -> None:
    model = _snapshot().models[0]
    snapshot = replace(
        _snapshot(),
        models=(
            replace(
                model,
                provenance={
                    name: value for name, value in model.provenance.items() if name != "reasoning"
                },
            ),
        ),
    )

    with pytest.raises(SnapshotValidationError, match="reasoning"):
        validate_snapshot(snapshot, (_source_spec(),))


def test_fast_route_requires_standard_pair_or_source_declaration() -> None:
    fast_route = _route(SpeedMode.FAST, selector="model-fast")
    fast_snapshot = replace(
        _snapshot(),
        models=(replace(_snapshot().models[0], routes=(fast_route,)),),
    )

    with pytest.raises(SnapshotValidationError, match="standard route"):
        validate_snapshot(fast_snapshot, (_source_spec(),))

    assert (
        validate_snapshot(
            fast_snapshot,
            (_source_spec(fast_only_selectors=frozenset({"model-fast"})),),
        )
        is fast_snapshot
    )
    paired_snapshot = replace(
        fast_snapshot,
        models=(replace(fast_snapshot.models[0], routes=(_route(), fast_route)),),
    )
    assert validate_snapshot(paired_snapshot, (_source_spec(),)) is paired_snapshot
