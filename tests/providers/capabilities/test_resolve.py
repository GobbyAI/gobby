from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
from gobby.providers.capabilities.resolve import (
    CapabilityResolver,
    ContextSource,
    ReasoningStatus,
    SpeedStatus,
)


class _CapabilityStore:
    def __init__(self, snapshot: ProviderSnapshot | None) -> None:
        self.snapshot = snapshot

    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None:
        if self.snapshot is None or self.snapshot.provider != provider:
            return None
        return self.snapshot


class _ModelMetadataStore:
    def __init__(self, context_length: int | None | dict[str, int]) -> None:
        self.context_lengths = (
            context_length
            if isinstance(context_length, dict)
            else ({"model": context_length} if context_length is not None else {})
        )

    def get_context_window(self, model: str) -> int | None:
        return self.context_lengths.get(model)


class _ConfigStore:
    def __init__(self, aliases: list[dict[str, str]] | None = None) -> None:
        self.aliases = aliases

    def get(self, key: str) -> object | None:
        return self.aliases


def _snapshot(
    *,
    context_length: int | None = 128_000,
    reasoning: ReasoningSupport = ReasoningSupport.KNOWN,
    supported_efforts: tuple[str, ...] | None = ("low", "medium", "high"),
    include_fast: bool = True,
) -> ProviderSnapshot:
    observed_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    provenance = {
        "model": FactProvenance(
            source_key="provider-api",
            source_url="https://example.test/models",
            observed_at=observed_at,
        )
    }
    routes = [
        ModelRoute(
            speed_mode=SpeedMode.STANDARD,
            selector="model-standard",
            available=True,
            usage_multiplier=Decimal("1"),
            throughput_multiplier=None,
            latency_class="normal",
            activations=(),
            provenance=provenance,
        )
    ]
    if include_fast:
        routes.append(
            ModelRoute(
                speed_mode=SpeedMode.FAST,
                selector="model-fast",
                available=True,
                usage_multiplier=Decimal("1.5"),
                throughput_multiplier=Decimal("4"),
                latency_class="fastest",
                activations=(
                    ActivationDescriptor(kind="model_selector", surface="spawn-cli", params={}),
                    ActivationDescriptor(kind="cli_config", surface="app-server", params={}),
                    ActivationDescriptor(kind="env", surface="spawn-cli", params={}),
                ),
                provenance=provenance,
            )
        )
    model = ModelCapability(
        canonical_model="model",
        display_name="Model",
        aliases=("model-latest",),
        available=True,
        hidden=False,
        is_default=True,
        context_length=context_length,
        max_output_tokens=16_000,
        reasoning=reasoning,
        supported_efforts=supported_efforts,
        default_effort="medium",
        latency_class="normal",
        input_modalities=("text",),
        supports_tools=True,
        routes=tuple(routes),
        provenance=provenance,
    )
    source = SourceHealth(
        source_key="provider-api",
        source_url="https://example.test/models",
        required=True,
        state=SourceState.OK,
        attempts=1,
        last_attempt_at=observed_at,
        last_success_at=observed_at,
        last_error=None,
    )
    return ProviderSnapshot(provider="provider", generation=1, models=(model,), sources=(source,))


def test_context_precedence_order() -> None:
    resolver = CapabilityResolver(_CapabilityStore(_snapshot()), _ModelMetadataStore(32_000))

    caller = resolver.resolve_context(
        "provider", "model", caller_override=256_000, route_override=192_000
    )
    route = resolver.resolve_context("provider", "model", route_override=192_000)
    matrix = resolver.resolve_context("provider", "model")
    metadata = CapabilityResolver(
        _CapabilityStore(_snapshot(context_length=None)), _ModelMetadataStore(32_000)
    ).resolve_context("provider", "model")
    unknown = CapabilityResolver(
        _CapabilityStore(_snapshot(context_length=None)), _ModelMetadataStore(None)
    ).resolve_context("provider", "model")

    assert (caller.value, caller.source) == (256_000, ContextSource.CALLER_OVERRIDE)
    assert (route.value, route.source) == (192_000, ContextSource.ROUTE_OVERRIDE)
    assert (matrix.value, matrix.source) == (128_000, ContextSource.PROVIDER_MATRIX)
    assert (metadata.value, metadata.source) == (32_000, ContextSource.OPENROUTER)
    assert (unknown.value, unknown.source) == (None, ContextSource.UNKNOWN)


def test_direct_metadata_match_precedes_provider_alias() -> None:
    metadata = _ModelMetadataStore({"model": 32_000, "registry-model": 64_000})
    config = _ConfigStore(
        [
            {
                "provider": "provider",
                "provider_model_id": "model",
                "openrouter_model_id": "registry-model",
            }
        ]
    )
    resolver = CapabilityResolver(
        _CapabilityStore(_snapshot(context_length=None)),
        metadata,
        config,
    )

    result = resolver.resolve_context("provider", "model")

    assert (result.value, result.source) == (32_000, ContextSource.OPENROUTER)


def test_provider_alias_is_scoped_and_config_edits_are_live() -> None:
    metadata = _ModelMetadataStore({"registry-model": 64_000})
    config = _ConfigStore(
        [
            {
                "provider": "other-provider",
                "provider_model_id": "model",
                "openrouter_model_id": "registry-model",
            }
        ]
    )
    resolver = CapabilityResolver(
        _CapabilityStore(_snapshot(context_length=None)),
        metadata,
        config,
    )

    assert resolver.resolve_context("provider", "model").source is ContextSource.UNKNOWN

    config.aliases = [
        {
            "provider": " Provider ",
            "provider_model_id": " MODEL ",
            "openrouter_model_id": "registry-model",
        }
    ]
    resolved = resolver.resolve_context("provider", "model")
    assert (resolved.value, resolved.source) == (64_000, ContextSource.OPENROUTER)

    config.aliases = []
    assert resolver.resolve_context("provider", "model").source is ContextSource.UNKNOWN


def test_missing_alias_target_remains_unknown() -> None:
    config = _ConfigStore(
        [
            {
                "provider": "provider",
                "provider_model_id": "model",
                "openrouter_model_id": "missing-registry-model",
            }
        ]
    )
    resolver = CapabilityResolver(
        _CapabilityStore(_snapshot(context_length=None)),
        _ModelMetadataStore(None),
        config,
    )

    result = resolver.resolve_context("provider", "model")

    assert (result.value, result.source) == (None, ContextSource.UNKNOWN)


def test_reasoning_tristate() -> None:
    unsupported = CapabilityResolver(
        _CapabilityStore(_snapshot(reasoning=ReasoningSupport.UNSUPPORTED)),
        _ModelMetadataStore(None),
    ).resolve_reasoning("provider", "model", "high", transport_supports_effort=True)
    outside_known_set = CapabilityResolver(
        _CapabilityStore(_snapshot()), _ModelMetadataStore(None)
    ).resolve_reasoning("provider", "model", "max", transport_supports_effort=True)
    unknown = CapabilityResolver(
        _CapabilityStore(_snapshot(reasoning=ReasoningSupport.UNKNOWN, supported_efforts=None)),
        _ModelMetadataStore(None),
    ).resolve_reasoning("provider", "model", "max", transport_supports_effort=True)

    assert unsupported.status is ReasoningStatus.REJECTED
    assert outside_known_set.status is ReasoningStatus.REJECTED
    assert unknown.status is ReasoningStatus.UNVERIFIED
    assert unknown.effective_effort == "max"


def test_fast_unavailable_pre_dispatch() -> None:
    without_fast = CapabilityResolver(
        _CapabilityStore(_snapshot(include_fast=False)), _ModelMetadataStore(None)
    ).resolve_route("provider", "model", SpeedMode.FAST, "spawn-cli")
    wrong_surface = CapabilityResolver(
        _CapabilityStore(_snapshot()), _ModelMetadataStore(None)
    ).resolve_route("provider", "model", SpeedMode.FAST, "tool-chat")

    assert without_fast.status is SpeedStatus.FAST_UNAVAILABLE
    assert wrong_surface.status is SpeedStatus.FAST_UNAVAILABLE
    assert without_fast.effective is SpeedMode.STANDARD
    assert wrong_surface.effective is SpeedMode.STANDARD


def test_route_resolution_surface_filtering() -> None:
    resolver = CapabilityResolver(_CapabilityStore(_snapshot()), _ModelMetadataStore(None))

    standard = resolver.resolve_route("provider", "model", surface="spawn-cli")
    fast = resolver.resolve_route("provider", "model-latest", SpeedMode.FAST, "spawn-cli")

    assert standard.status is SpeedStatus.STANDARD
    assert standard.selector == "model-standard"
    assert fast.status is SpeedStatus.FAST_CONFIGURED
    assert fast.selector == "model-fast"
    assert [activation.kind for activation in fast.activations] == ["model_selector", "env"]
