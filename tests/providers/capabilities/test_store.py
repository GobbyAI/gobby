"""PostgreSQL persistence tests for provider capability snapshots."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from psycopg.errors import UniqueViolation

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
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


def _snapshot(
    provider: str = "codex",
    model_name: str = "gpt-test",
    *,
    generation: int = 41,
) -> ProviderSnapshot:
    observed_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    source_url = f"https://example.test/{provider}/models"
    provenance = {
        "catalog": FactProvenance(
            source_key="provider-api",
            source_url=source_url,
            observed_at=observed_at,
        )
    }
    route = ModelRoute(
        speed_mode=SpeedMode.STANDARD,
        selector=model_name,
        available=True,
        usage_multiplier=Decimal("1"),
        throughput_multiplier=None,
        latency_class="normal",
        activations=(),
        provenance=provenance,
    )
    model = ModelCapability(
        canonical_model=model_name,
        display_name=f"{provider.title()} Test",
        aliases=(f"{model_name}-latest",),
        available=True,
        hidden=False,
        is_default=True,
        context_length=128_000,
        max_output_tokens=16_000,
        reasoning=ReasoningSupport.KNOWN,
        supported_efforts=("low", "medium", "high"),
        default_effort="medium",
        latency_class="normal",
        input_modalities=("text", "image"),
        supports_tools=True,
        routes=(route,),
        provenance=provenance,
    )
    source = SourceHealth(
        source_key="provider-api",
        source_url=source_url,
        required=True,
        state=SourceState.OK,
        attempts=1,
        last_attempt_at=observed_at,
        last_success_at=observed_at,
        last_error=None,
    )
    return ProviderSnapshot(
        provider=provider,
        generation=generation,
        models=(model,),
        sources=(source,),
    )


def test_failed_replace_retains_last_good_rows(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(_snapshot())
    before = store.get_provider_snapshot("codex")
    assert before is not None

    replacement = _snapshot(model_name="gpt-broken")
    route = replacement.models[0].routes[0]
    duplicate_route = replace(route, selector="duplicate-selector")
    broken_model = replace(replacement.models[0], routes=(route, duplicate_route))

    with pytest.raises(UniqueViolation):
        store.replace_provider_snapshot(replace(replacement, models=(broken_model,)))

    assert store.get_provider_snapshot("codex") == before


def test_capability_rows_independent_of_model_metadata(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    postgres_db.execute("DELETE FROM model_metadata")

    store.replace_provider_snapshot(_snapshot())

    metadata_count = postgres_db.fetchone("SELECT COUNT(*) AS count FROM model_metadata")
    assert metadata_count is not None
    assert metadata_count["count"] == 0
    assert store.get_provider_snapshot("codex") == replace(_snapshot(), generation=1)


def test_source_failure_updates_health_only(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(_snapshot())
    before = store.get_provider_snapshot("codex")
    assert before is not None

    store.record_source_failure("codex", "provider-api", "upstream unavailable")

    after = store.get_provider_snapshot("codex")
    assert after is not None
    assert after.models == before.models
    assert after.generation == before.generation
    assert after.sources[0].state is SourceState.STALE
    assert after.sources[0].attempts == 2
    assert after.sources[0].last_error == "upstream unavailable"


def test_source_failure_without_rows_is_error(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)

    store.record_source_failure("extension", "provider-api", "not reachable")

    snapshot = store.get_provider_snapshot("extension")
    assert snapshot is not None
    assert snapshot.models == ()
    assert snapshot.generation == 0
    assert snapshot.sources[0].state is SourceState.ERROR
    assert snapshot.sources[0].attempts == 1


def test_replace_bumps_generation_and_replaces_provider_rows(
    postgres_db: HubDatabase,
) -> None:
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(_snapshot(model_name="first"))

    store.replace_provider_snapshot(_snapshot(model_name="second"))

    snapshot = store.get_provider_snapshot("codex")
    assert snapshot is not None
    assert snapshot.generation == 2
    assert [model.canonical_model for model in snapshot.models] == ["second"]
    assert store.has_rows("codex") is True
    assert store.has_rows("claude") is False


def test_mark_stale_preserves_capability_rows(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(_snapshot())
    before = store.get_provider_snapshot("codex")
    assert before is not None

    store.mark_stale("codex")

    after = store.get_provider_snapshot("codex")
    assert after is not None
    assert after.models == before.models
    assert after.sources[0].state is SourceState.STALE


def test_all_snapshots_follow_provider_display_order(postgres_db: HubDatabase) -> None:
    store = ProviderCapabilityStore(postgres_db)
    store.replace_provider_snapshot(_snapshot(provider="extension", model_name="extension-model"))
    store.replace_provider_snapshot(_snapshot(provider="qwen", model_name="qwen-model"))
    store.replace_provider_snapshot(_snapshot(provider="claude", model_name="claude-model"))

    snapshots = store.get_all_snapshots()

    assert [snapshot.provider for snapshot in snapshots] == ["claude", "qwen", "extension"]
