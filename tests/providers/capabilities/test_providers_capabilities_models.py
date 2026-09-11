"""Tests for typed provider capability domain models."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from gobby.providers.capabilities.local_context import (
    LocalContextInstance,
    build_context_observation,
)
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


def test_local_context_provenance_round_trip_and_remote_omission() -> None:
    remote = _snapshot(None)
    remote_payload = remote.to_dict()
    assert "local_context" not in remote_payload["models"][0]["provenance"]["context_length"]

    observation = build_context_observation(
        machine_id="machine-a",
        endpoint_id="studio-a",
        configuration_fingerprint="config-a",
        provider="lmstudio",
        model_id="model",
        instance_id="instance-a",
        digest="sha256:model",
        canonical_limit=262_144,
        observed_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        provenance={"canonical_limit": "/api/v1/models/0/max_context_length"},
        instances=(
            LocalContextInstance(
                model_id="model",
                instance_id="instance-a",
                digest="sha256:model",
                canonical_limit=262_144,
                runtime_limit=32_768,
                provenance={"runtime_limit": "/api/v1/models/0/loaded_instances/0"},
            ),
        ),
    )
    fact = FactProvenance(
        source_key="local-context",
        source_url="http://localhost:1234/v1",
        observed_at=observation.observed_at,
        local_context=observation,
    )
    local = replace(
        remote,
        models=(replace(remote.models[0], provenance={"context_length": fact}),),
    )

    payload = local.to_dict()

    assert payload["models"][0]["provenance"]["context_length"]["local_context"] == (
        observation.to_dict()
    )
    assert ProviderSnapshot.from_dict(payload) == local
