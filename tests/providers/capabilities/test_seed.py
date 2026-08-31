"""Bundled provider capability seed behavior."""

from datetime import UTC, datetime

import pytest

from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
    SpeedMode,
)
from gobby.providers.capabilities.seed import _agy_snapshot, apply_seed
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


def _live_claude_snapshot() -> ProviderSnapshot:
    observed_at = datetime(2026, 8, 4, tzinfo=UTC)
    provenance = FactProvenance(
        source_key="models-overview",
        source_url="https://example.test/models-overview",
        observed_at=observed_at,
    )
    model = ModelCapability(
        canonical_model="claude-live",
        display_name="Claude Live",
        aliases=("opus",),
        available=True,
        hidden=False,
        is_default=True,
        context_length=200_000,
        max_output_tokens=64_000,
        reasoning=ReasoningSupport.KNOWN,
        supported_efforts=("low", "medium", "high"),
        default_effort="high",
        latency_class=None,
        input_modalities=("text", "image"),
        supports_tools=None,
        routes=(
            ModelRoute(
                speed_mode=SpeedMode.STANDARD,
                selector="claude-live",
                available=True,
                usage_multiplier=None,
                throughput_multiplier=None,
                latency_class=None,
                activations=(),
                provenance={"selector": provenance},
            ),
        ),
        provenance={"canonical_model": provenance},
    )
    sources = tuple(
        SourceHealth(
            source_key=source_key,
            source_url=f"https://example.test/{source_key}",
            required=True,
            state=SourceState.OK,
            attempts=1,
            last_attempt_at=observed_at,
            last_success_at=observed_at,
            last_error=None,
        )
        for source_key in ("models-overview", "model-config", "effort-docs")
    )
    return ProviderSnapshot(provider="claude", generation=0, models=(model,), sources=sources)


def test_seed_only_when_empty(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(_live_claude_snapshot())

    apply_seed(store)

    claude = store.get_provider_snapshot("claude")
    droid = store.get_provider_snapshot("droid")
    assert claude is not None
    assert [model.canonical_model for model in claude.models] == ["claude-live"]
    assert droid is not None
    assert len(droid.models) == 30
    assert {source.state for source in droid.sources} == {SourceState.STALE}
    assert all(
        provenance.source_key == "bundled"
        for model in droid.models
        for provenance in model.provenance.values()
    )

    models = {model.canonical_model: model for model in droid.models}
    paired = models["claude-opus-5"]
    assert [(route.speed_mode, route.selector) for route in paired.routes] == [
        (SpeedMode.STANDARD, "claude-opus-5"),
        (SpeedMode.FAST, "claude-opus-5-fast"),
    ]
    standalone_fast = models["claude-opus-4-6-fast"]
    assert [route.speed_mode for route in standalone_fast.routes] == [SpeedMode.STANDARD]


def test_seed_includes_agy_floor_catalog(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)

    apply_seed(store)

    snapshot = store.get_provider_snapshot("agy")
    assert snapshot is not None
    assert {model.canonical_model for model in snapshot.models} == {
        "gemini-3.7-flash-high",
        "gemini-3.7-flash-medium",
        "gemini-3.7-flash-low",
        "gemini-3.6-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.6-flash-low",
        "gemini-3.5-flash-high",
        "gemini-3.5-flash-medium",
        "gemini-3.5-flash-low",
        "gemini-3.1-pro-high",
        "gemini-3.1-pro-low",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
        "gpt-oss-120b-medium",
    }
    assert [(source.source_key, source.state) for source in snapshot.sources] == [
        ("bundled", SourceState.STALE)
    ]
    models = {model.canonical_model: model for model in snapshot.models}
    assert models["gemini-3.5-flash-high"].aliases == ("gemini-3.5-flash",)
    assert models["gemini-3.5-flash-high"].context_length == 1_048_576
    assert all(
        provenance.source_key == "bundled"
        for model in snapshot.models
        for provenance in model.provenance.values()
    )


def test_agy_seed_snapshot_is_fixture_derived() -> None:
    snapshot = _agy_snapshot(datetime(2026, 8, 20, 12, 0, tzinfo=UTC))

    assert len(snapshot.models) == 14
    assert snapshot.models[0].canonical_model == "gemini-3.7-flash-high"
    assert snapshot.models[-1].canonical_model == "gpt-oss-120b-medium"
    assert snapshot.models[0].routes[0].selector == "gemini-3.7-flash-high"


def test_refresh_replaces_seed(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    apply_seed(store)
    seeded = store.get_provider_snapshot("claude")
    assert seeded is not None
    assert {source.state for source in seeded.sources} == {SourceState.STALE}
    seeded_models = {model.canonical_model: model for model in seeded.models}
    assert set(seeded_models) == {
        "claude-fable-5",
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
    }
    assert seeded_models["claude-fable-5"].aliases == ("fable",)
    assert seeded_models["claude-opus-5"].aliases == (
        "opus",
        "opus[1m]",
        "opusplan",
    )
    assert seeded_models["claude-sonnet-5"].aliases == ("sonnet", "sonnet[1m]")
    assert seeded_models["claude-haiku-4-5-20251001"].aliases == ("haiku",)
    assert all(
        model.supported_efforts == ("low", "medium", "high", "xhigh", "max")
        for model in seeded.models
    )

    store.replace_provider_snapshot(_live_claude_snapshot())

    refreshed = store.get_provider_snapshot("claude")
    assert refreshed is not None
    assert [model.canonical_model for model in refreshed.models] == ["claude-live"]
    assert {source.state for source in refreshed.sources} == {SourceState.OK}
