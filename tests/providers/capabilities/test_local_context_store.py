"""PostgreSQL storage tests for endpoint-scoped local context observations."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.providers.capabilities.local_context import (
    ContextDiagnostic,
    LocalContextInstance,
    LocalContextObservation,
    build_context_observation,
)
from gobby.providers.capabilities.local_context_store import (
    LocalContextStore,
    local_provider_namespace,
)
from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
)
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.integration

_OBSERVED_AT = datetime(2026, 9, 11, 12, tzinfo=UTC)


def _observation(
    *,
    machine_id: str = "machine-a",
    endpoint_id: str = "studio-a",
    configuration_fingerprint: str = "credential-aware-fingerprint",
    model_id: str = "shared/model",
    instance_id: str | None = "instance-a",
    digest: str | None = "sha256:model",
    runtime_limit: int | None = 32_768,
    diagnostics: tuple[ContextDiagnostic, ...] = (),
) -> LocalContextObservation:
    instances = (
        LocalContextInstance(
            model_id=model_id,
            instance_id=instance_id,
            digest=digest,
            canonical_limit=262_144,
            runtime_limit=runtime_limit,
            provenance={"runtime_limit": "/api/v1/models/0/loaded_instances/0"},
        ),
    )
    return build_context_observation(
        machine_id=machine_id,
        endpoint_id=endpoint_id,
        configuration_fingerprint=configuration_fingerprint,
        provider="lmstudio",
        model_id=model_id,
        instance_id=instance_id,
        digest=digest,
        canonical_limit=262_144,
        observed_at=_OBSERVED_AT,
        provenance={"canonical_limit": "/api/v1/models/0/max_context_length"},
        diagnostics=diagnostics,
        instances=instances,
    )


def _replace(
    store: LocalContextStore,
    observation: LocalContextObservation,
    *,
    base_url: str = "http://localhost:1234/v1",
) -> None:
    store.replace_endpoint_snapshot(
        machine_id=observation.machine_id,
        endpoint_id=observation.endpoint_id,
        configuration_fingerprint=observation.configuration_fingerprint,
        base_url=base_url,
        observations=(observation,),
    )


def _remote_snapshot() -> ProviderSnapshot:
    fact = FactProvenance(
        source_key="provider-api",
        source_url="https://example.test/models",
        observed_at=_OBSERVED_AT,
    )
    return ProviderSnapshot(
        provider="openai-compatible",
        generation=0,
        models=(
            ModelCapability(
                canonical_model="shared/model",
                display_name="Shared Model",
                aliases=(),
                available=True,
                hidden=False,
                is_default=False,
                context_length=128_000,
                max_output_tokens=None,
                reasoning=ReasoningSupport.UNKNOWN,
                supported_efforts=None,
                default_effort=None,
                latency_class=None,
                input_modalities=None,
                supports_tools=None,
                provenance={"context_length": fact},
            ),
        ),
        sources=(),
    )


def test_observation_storage_roundtrip(postgres_db: HubDatabase) -> None:
    capability_store = ProviderCapabilityStore(postgres_db)
    store = LocalContextStore(capability_store)
    known = _observation()
    other_instance = _observation(
        instance_id="instance-b",
        digest="sha256:model-b",
        runtime_limit=16_384,
    )
    unknown = _observation(
        model_id="unknown/model",
        instance_id=None,
        digest=None,
        runtime_limit=None,
        diagnostics=(ContextDiagnostic.ENDPOINT_UNAVAILABLE,),
    )

    store.replace_endpoint_snapshot(
        machine_id=known.machine_id,
        endpoint_id=known.endpoint_id,
        configuration_fingerprint=known.configuration_fingerprint,
        base_url=(
            "http://private-user:private-password@localhost:1234/v1?api_key=private-token#private"
        ),
        observations=(known, other_instance, unknown),
    )

    assert store.get_endpoint_observations(
        machine_id=known.machine_id,
        endpoint_id=known.endpoint_id,
        configuration_fingerprint=known.configuration_fingerprint,
    ) == (known, other_instance, unknown)
    assert known.canonical_limit == 262_144
    assert known.runtime_limit == 32_768
    assert known.effective_limit == 32_768
    assert known.instances[0].instance_id == "instance-a"
    assert known.instances[0].digest == "sha256:model"
    assert (
        store.get_observation(
            machine_id=other_instance.machine_id,
            endpoint_id=other_instance.endpoint_id,
            configuration_fingerprint=other_instance.configuration_fingerprint,
            model_id=other_instance.model_id,
            instance_id=other_instance.instance_id,
            digest=other_instance.digest,
        )
        == other_instance
    )
    assert unknown.effective_limit is None
    assert ContextDiagnostic.ENDPOINT_UNAVAILABLE in unknown.diagnostics
    assert ContextDiagnostic.RUNTIME_UNKNOWN in unknown.diagnostics

    namespace = local_provider_namespace(
        known.machine_id,
        known.endpoint_id,
        known.configuration_fingerprint,
    )
    snapshot = capability_store.get_provider_snapshot(namespace)
    assert snapshot is not None
    facts = tuple(model.provenance["context_length"] for model in snapshot.models)
    assert {fact.source_url for fact in facts} == {"http://localhost:1234/v1"}
    assert {fact.local_context.endpoint_id for fact in facts if fact.local_context} == {"studio-a"}
    serialized = json.dumps(snapshot.to_dict())
    assert "private-user" not in serialized
    assert "private-password" not in serialized
    assert "private-token" not in serialized


def test_observation_storage_isolation(postgres_db: HubDatabase) -> None:
    capability_store = ProviderCapabilityStore(postgres_db)
    store = LocalContextStore(capability_store)
    original = _observation()
    other_endpoint = _observation(endpoint_id="studio-b")
    other_machine = _observation(machine_id="machine-b")
    changed_credentials = _observation(configuration_fingerprint="changed-credentials")
    for observation in (original, other_endpoint, other_machine, changed_credentials):
        _replace(store, observation)
    capability_store.replace_provider_snapshot(_remote_snapshot())
    remote_before = capability_store.get_provider_snapshot("openai-compatible")

    replacement = _observation(model_id="replacement/model", instance_id="new-instance")
    _replace(store, replacement)

    assert (
        store.get_observation(
            machine_id=original.machine_id,
            endpoint_id=original.endpoint_id,
            configuration_fingerprint=original.configuration_fingerprint,
            model_id=original.model_id,
            instance_id=original.instance_id,
            digest=original.digest,
        )
        is None
    )
    assert store.get_endpoint_observations(
        machine_id=replacement.machine_id,
        endpoint_id=replacement.endpoint_id,
        configuration_fingerprint=replacement.configuration_fingerprint,
    ) == (replacement,)
    for untouched in (other_endpoint, other_machine, changed_credentials):
        assert store.get_endpoint_observations(
            machine_id=untouched.machine_id,
            endpoint_id=untouched.endpoint_id,
            configuration_fingerprint=untouched.configuration_fingerprint,
        ) == (untouched,)
    assert capability_store.get_provider_snapshot("openai-compatible") == remote_before
