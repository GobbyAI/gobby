"""Pure evidence tests: no endpoints, daemon state, or provider catalogs."""

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest

from gobby.providers.capabilities.local_context import (
    ContextDiagnostic,
    LocalContextInstance,
    LocalContextObservation,
    build_context_observation,
    effective_local_limit,
    positive_limit,
)

pytestmark = pytest.mark.unit


def _observation(
    *instances: LocalContextInstance,
    provider: str = "lmstudio",
    canonical: object = 262_144,
    selected: str | None = None,
    digest: str | None = None,
) -> LocalContextObservation:
    return build_context_observation(
        machine_id="machine-a",
        endpoint_id="studio-a",
        configuration_fingerprint="config-a",
        provider=provider,
        model_id="model",
        instance_id=selected,
        digest=digest,
        canonical_limit=canonical,
        instances=instances,
        observed_at=datetime(2026, 9, 11, 12, tzinfo=UTC),
        provenance={"canonical_limit": "/api/v1/models/models/0/max_context_length"},
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1),
        (2_147_483_647, 2_147_483_647),
        ("32768", 32768),
        ("00012", 12),
        ("0" * 5000 + "1", 1),
        (True, None),
        (False, None),
        (0, None),
        (-1, None),
        (1.0, None),
        (float("inf"), None),
        (float("nan"), None),
        (None, None),
        ("0", None),
        ("-1", None),
        ("+1", None),
        (" 1", None),
        ("1 ", None),
        ("1.0", None),
        ("1e5", None),
        ("", None),
        ("１２", None),
        ("١٢", None),
        ("²", None),
        ("9" * 5000, None),
        (2_147_483_648, None),
        ("2147483648", None),
        ([], None),
        ({}, None),
    ],
)
def test_positive_limit(value: object, expected: int | None) -> None:
    assert positive_limit(value) == expected


def test_effective_limits_and_overrides() -> None:
    evidence = _observation(LocalContextInstance(model_id="model", runtime_limit=32_768))
    assert (evidence.canonical_limit, evidence.runtime_limit, evidence.effective_limit) == (
        262_144,
        32_768,
        32_768,
    )
    assert effective_local_limit(evidence, 262_144, 65_536) == 32_768
    assert effective_local_limit(evidence, 16_384, 8_192) == 8_192
    assert effective_local_limit(evidence, True, 0, -10, "invalid") == 32_768
    assert effective_local_limit(evidence, "4096") == 4_096
    assert effective_local_limit(_observation(), 262_144) is None
    assert effective_local_limit(None, 32_768) is None


def test_instance_evidence() -> None:
    small = LocalContextInstance(model_id="model", instance_id="small", runtime_limit=32_768)
    large = LocalContextInstance(model_id="model", instance_id="large", runtime_limit=131_072)
    missing = LocalContextInstance(model_id="model", instance_id="unknown")
    assert _observation(small, large).effective_limit == 32_768
    assert _observation(small, large, missing, selected="large").effective_limit == 131_072
    aggregate = _observation(small, large, missing)
    assert aggregate.runtime_limit is None
    assert effective_local_limit(aggregate, 4_096) is None
    assert ContextDiagnostic.RUNTIME_UNKNOWN in aggregate.diagnostics
    absent = _observation(small, selected="absent")
    assert absent.effective_limit is None
    assert ContextDiagnostic.INSTANCE_MISSING in absent.diagnostics
    assert len(aggregate.instances) == 3


@pytest.mark.parametrize("provider", ["lmstudio", "ollama", " LMStudio "])
def test_native_canonical_evidence_required(provider: str) -> None:
    instance = LocalContextInstance(model_id="model", runtime_limit=32_768)
    evidence = _observation(instance, provider=provider, canonical=None)
    assert evidence.runtime_limit == 32_768
    assert evidence.effective_limit is None
    assert ContextDiagnostic.CANONICAL_UNKNOWN in evidence.diagnostics
    per_instance = replace(instance, canonical_limit=16_384)
    assert _observation(per_instance, provider=provider, canonical=None).effective_limit == 16_384


@pytest.mark.parametrize("provider", ["vllm", "openai-compatible", "responses"])
def test_serving_only_evidence(provider: str) -> None:
    instance = LocalContextInstance(model_id="model", runtime_limit=32_768)
    assert _observation(instance, provider=provider, canonical=None).effective_limit == 32_768
    assert _observation(provider=provider, canonical=262_144).effective_limit is None


def test_invalid_evidence_roundtrip() -> None:
    instance = LocalContextInstance(
        model_id="model",
        instance_id="loaded",
        digest="sha256:a",
        canonical_limit=65_536,
        runtime_limit=131_072,
        provenance={
            "runtime_limit": "/api/v1/models/models/0/loaded_instances/0/config/context_length"
        },
    )
    evidence = _observation(instance, canonical=32_768, digest="sha256:a")
    assert evidence.runtime_limit == 131_072
    assert evidence.effective_limit == 32_768
    assert ContextDiagnostic.LIMIT_CONFLICT in evidence.diagnostics
    assert LocalContextObservation.from_dict(json.loads(json.dumps(evidence.to_dict()))) == evidence
    for other in (
        replace(instance, model_id="another-model"),
        replace(instance, digest="sha256:b"),
    ):
        unknown = _observation(instance, other)
        assert unknown.effective_limit is None
        assert ContextDiagnostic.IDENTITY_CONFLICT in unknown.diagnostics
        assert LocalContextObservation.from_dict(unknown.to_dict()) == unknown
    invalid = _observation(instance, canonical=True)
    assert invalid.effective_limit is None
    assert ContextDiagnostic.INVALID_METADATA in invalid.diagnostics
    assert LocalContextObservation.from_dict(invalid.to_dict()) == invalid
    conflicting = _observation(
        replace(instance, runtime_limit=32_768),
        replace(instance, instance_id="other", canonical_limit=16_384, runtime_limit=8_192),
        canonical=None,
    )
    assert conflicting.effective_limit == 8_192
    assert ContextDiagnostic.LIMIT_CONFLICT in conflicting.diagnostics


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "123", 2_147_483_648])
def test_domain_instances_reject_unnormalized_values(value: object) -> None:
    with pytest.raises(ValueError, match="runtime_limit must be"):
        LocalContextInstance(model_id="model", runtime_limit=cast(int, value))


def test_instance_diagnostics_and_identity_isolation() -> None:
    good = LocalContextInstance(model_id="model", instance_id="good", runtime_limit=32_768)
    bad = LocalContextInstance(
        model_id="model",
        instance_id="bad",
        runtime_limit=32_768,
        diagnostics=(ContextDiagnostic.INVALID_METADATA,),
    )
    assert _observation(good, bad).effective_limit is None
    assert _observation(good, bad, selected="good").effective_limit == 32_768
    first = _observation(good)
    assert replace(first, machine_id="other-machine") != first
    assert replace(first, endpoint_id="other-endpoint") != first
    assert replace(first, configuration_fingerprint="other-config") != first


def test_observations_are_immutable_and_utc() -> None:
    sources = {"runtime_limit": "/api/ps/models/0/context_length"}
    instance = LocalContextInstance(model_id="model", runtime_limit=32_768, provenance=sources)
    sources["runtime_limit"] = "changed"
    assert instance.provenance["runtime_limit"] == "/api/ps/models/0/context_length"
    evidence = _observation(instance)
    with pytest.raises(FrozenInstanceError):
        cast(Any, evidence).effective_limit = 999_999
    with pytest.raises(TypeError):
        cast(dict[str, str], evidence.provenance)["canonical_limit"] = "changed"
    shifted = replace(
        evidence, observed_at=datetime(2026, 9, 11, 7, tzinfo=timezone(timedelta(hours=-5)))
    )
    assert shifted.observed_at == datetime(2026, 9, 11, 12, tzinfo=UTC)
    assert shifted.observed_at.tzinfo is UTC
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(evidence, observed_at=datetime(2026, 9, 11))


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("runtime_limit", 999_999),
        ("effective_limit", 999_999),
        ("effective_limit", True),
        ("canonical_limit", "262144"),
        ("machine_id", ""),
        ("model_id", None),
        ("observed_at", "2026-09-11T12:00:00"),
        ("instances", {}),
        ("instances", [{}]),
        ("diagnostics", "context_runtime_unknown"),
        ("diagnostics", ["unknown-diagnostic"]),
        ("provenance", {"field": 12}),
        ("unexpected", 1),
    ],
)
def test_deserialization_rejects_malformed_or_forged_evidence(key: str, value: Any) -> None:
    data = _observation(LocalContextInstance(model_id="model", runtime_limit=32_768)).to_dict()
    data[key] = value
    with pytest.raises(ValueError):
        LocalContextObservation.from_dict(data)


def test_deserialization_requires_complete_evidence() -> None:
    data = _observation().to_dict()
    del data["instances"]
    with pytest.raises(ValueError, match="evidence fields"):
        LocalContextObservation.from_dict(data)
