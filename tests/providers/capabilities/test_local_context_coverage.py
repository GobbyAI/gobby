from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from gobby.providers.capabilities.coverage import ModelMetadataCoverageAuditor
from gobby.providers.capabilities.local_context_store import local_provider_namespace
from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
)

pytestmark = pytest.mark.unit


class _SnapshotStore:
    def __init__(self, *snapshots: ProviderSnapshot) -> None:
        self.snapshots = snapshots

    def get_all_snapshots(self) -> tuple[ProviderSnapshot, ...]:
        return self.snapshots


class _MetadataStore:
    def get_context_window(self, model: str) -> int | None:
        return None


def _snapshot(provider: str, *models: str) -> ProviderSnapshot:
    observed_at = datetime(2026, 9, 13, tzinfo=UTC)
    source = FactProvenance(
        source_key="catalog",
        source_url="https://example.test/models",
        observed_at=observed_at,
    )
    provenance = dict.fromkeys(
        (
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
        ),
        source,
    )
    return ProviderSnapshot(
        provider=provider,
        generation=0,
        models=tuple(
            ModelCapability(
                canonical_model=model,
                display_name=model,
                aliases=(),
                available=True,
                hidden=False,
                is_default=index == 0,
                context_length=None,
                max_output_tokens=None,
                reasoning=ReasoningSupport.UNKNOWN,
                supported_efforts=None,
                default_effort=None,
                latency_class=None,
                input_modalities=None,
                supports_tools=None,
                provenance=provenance,
            )
            for index, model in enumerate(models)
        ),
        sources=(
            SourceHealth(
                source_key="catalog",
                source_url="https://example.test/models",
                required=True,
                state=SourceState.OK,
                attempts=1,
                last_attempt_at=observed_at,
                last_success_at=observed_at,
                last_error=None,
            ),
        ),
    )


def test_mixed_local_remote_coverage(caplog: pytest.LogCaptureFixture) -> None:
    local_namespace = local_provider_namespace("machine-1", "studio", "fingerprint")
    store = _SnapshotStore(
        _snapshot("qwen", "LOCAL-MODEL", "remote-model"),
        _snapshot(local_namespace, "reserved-local-unknown"),
    )
    auditor = ModelMetadataCoverageAuditor(
        store,
        _MetadataStore(),
        [],
        excluded_models=lambda: frozenset({(" QWEN ", "local-model")}),
    )

    with caplog.at_level(logging.INFO, logger="gobby.providers.capabilities.coverage"):
        auditor.audit()

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["Provider qwen has 1 models without context metadata: remote-model"]
