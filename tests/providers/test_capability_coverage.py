"""Retired providers cannot persist or leak into the startup coverage audit."""

import logging

import pytest

from gobby.providers.capabilities.coverage import ModelMetadataCoverageAuditor
from gobby.providers.capabilities.local_context_store import local_provider_namespace
from gobby.providers.capabilities.models import (
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
)
from gobby.providers.capabilities.refresh import CapabilityRefreshCoordinator
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration


class _MetadataStore:
    def get_context_window(self, model: str) -> int | None:
        return None


def _snapshot(provider: str) -> ProviderSnapshot:
    return ProviderSnapshot(
        provider=provider,
        generation=0,
        models=(
            ModelCapability(
                canonical_model="test-model",
                display_name="Test model",
                aliases=(),
                available=True,
                hidden=False,
                is_default=True,
                context_length=None,
                max_output_tokens=None,
                reasoning=ReasoningSupport.UNKNOWN,
                supported_efforts=None,
                default_effort=None,
                latency_class=None,
                input_modalities=None,
                supports_tools=None,
                provenance={},
            ),
        ),
        sources=(
            SourceHealth(
                source_key="catalog",
                source_url=None,
                required=True,
                state=SourceState.OK,
                attempts=1,
                last_attempt_at=None,
                last_success_at=None,
                last_error=None,
            ),
        ),
    )


def _seed_retired_snapshot(db: HubDatabase, provider: str) -> None:
    """Simulate data written before a provider was removed from the registry."""
    ProviderCapabilityStore(db).replace_provider_snapshot(_snapshot("codex"))
    with db.transaction() as transaction:
        transaction.execute(
            "UPDATE provider_model_capabilities SET provider = %s WHERE provider = 'codex'",
            (provider,),
        )
        transaction.execute(
            "UPDATE provider_capability_refresh_state SET provider = %s WHERE provider = 'codex'",
            (provider,),
        )


@pytest.mark.parametrize("provider", ["qwen", "QWEN", "unsupported-provider"])
def test_snapshot_store_rejects_unsupported_provider(
    postgres_db: HubDatabase, provider: str
) -> None:
    store = ProviderCapabilityStore(postgres_db)

    with pytest.raises(ValueError, match="Unsupported capability provider"):
        store.replace_provider_snapshot(_snapshot(provider))
    with pytest.raises(ValueError, match="Unsupported capability provider"):
        store.record_source_failure(provider, "catalog", "failed")

    assert postgres_db.fetchall("SELECT provider FROM provider_model_capabilities") == []
    assert postgres_db.fetchall("SELECT provider FROM provider_capability_refresh_state") == []


def test_unsupported_provider_snapshot_is_not_audited(
    postgres_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_retired_snapshot(postgres_db, "qwen")
    store = ProviderCapabilityStore(postgres_db)
    auditor = ModelMetadataCoverageAuditor(store, _MetadataStore(), [])
    coordinator = CapabilityRefreshCoordinator(store, coverage_auditor=auditor)

    with caplog.at_level(logging.INFO):
        coordinator.prepare()
        coordinator.prepare()

    messages = [record.getMessage() for record in caplog.records]
    assert messages.count("Pruned 2 unsupported provider capability rows") == 1
    assert not any("Provider qwen" in message for message in messages)
    assert store.get_provider_snapshot("qwen") is None
    assert store.has_rows("qwen") is False
    assert (
        postgres_db.fetchall(
            "SELECT provider FROM provider_model_capabilities WHERE provider = 'qwen'"
        )
        == []
    )
    assert (
        postgres_db.fetchall(
            "SELECT provider FROM provider_capability_refresh_state WHERE provider = 'qwen'"
        )
        == []
    )


def test_snapshot_load_prunes_only_unsupported_providers(
    postgres_db: HubDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_retired_snapshot(postgres_db, "qwen")
    _seed_retired_snapshot(postgres_db, "another-retired-provider")
    store = ProviderCapabilityStore(postgres_db)
    local = local_provider_namespace("machine", "studio", "fingerprint")
    store.replace_provider_snapshot(_snapshot("claude"))
    store.replace_provider_snapshot(_snapshot(local))
    before = (store.get_provider_snapshot("claude"), store.get_provider_snapshot(local))

    with caplog.at_level(logging.INFO):
        loaded = store.get_all_snapshots()
        assert store.get_all_snapshots() == loaded

    assert loaded == before
    assert [record.getMessage() for record in caplog.records] == [
        "Pruned 4 unsupported provider capability rows"
    ]
    assert {
        row["provider"]
        for row in postgres_db.fetchall("SELECT provider FROM provider_model_capabilities")
    } == {"claude", local}
    assert {
        row["provider"]
        for row in postgres_db.fetchall("SELECT provider FROM provider_capability_refresh_state")
    } == {"claude", local}
