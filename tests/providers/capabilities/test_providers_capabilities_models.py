"""Tests for typed provider capability domain models."""

from datetime import UTC, datetime

import pytest

from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
    SourceHealth,
    SourceState,
)

pytestmark = pytest.mark.unit


def _snapshot(supported_efforts: tuple[str, ...] | None) -> ProviderSnapshot:
    observed_at = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    provenance = {
        "context_length": FactProvenance(
            source_key="provider-api",
            source_url="https://example.test/models",
            observed_at=observed_at,
        )
    }
    model = ModelCapability(
        canonical_model="model",
        display_name="Model",
        aliases=("model-latest",),
        available=True,
        hidden=False,
        is_default=True,
        context_length=200_000,
        max_output_tokens=32_000,
        reasoning=ReasoningSupport.KNOWN,
        supported_efforts=supported_efforts,
        default_effort="medium",
        latency_class="moderate",
        input_modalities=("text", "image"),
        supports_tools=True,
        provenance=provenance,
    )
    source = SourceHealth(
        source_key="provider-api",
        source_url="https://example.test/models",
        required=True,
        state=SourceState.OK,
        attempts=2,
        last_attempt_at=observed_at,
        last_success_at=observed_at,
        last_error=None,
    )
    return ProviderSnapshot(provider="provider", generation=7, models=(model,), sources=(source,))


def test_supported_efforts_null_vs_empty_distinct() -> None:
    unknown = _snapshot(None)
    explicitly_empty = _snapshot(())

    unknown_payload = unknown.to_dict()
    empty_payload = explicitly_empty.to_dict()

    assert unknown_payload["models"][0]["supported_efforts"] is None
    assert empty_payload["models"][0]["supported_efforts"] == []
    assert ProviderSnapshot.from_dict(unknown_payload) == unknown
    assert ProviderSnapshot.from_dict(empty_payload) == explicitly_empty
    assert unknown != explicitly_empty
