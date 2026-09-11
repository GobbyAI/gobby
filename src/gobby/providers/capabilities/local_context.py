"""Immutable endpoint-scoped evidence for local serving context limits."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ContextDiagnostic(StrEnum):
    CANONICAL_UNKNOWN = "context_canonical_unknown"
    RUNTIME_UNKNOWN = "context_runtime_unknown"
    INSTANCE_MISSING = "context_instance_missing"
    IDENTITY_CONFLICT = "context_identity_conflict"
    LIMIT_CONFLICT = "context_limit_conflict"
    INVALID_METADATA = "context_invalid_metadata"
    ENDPOINT_UNAVAILABLE = "context_endpoint_unavailable"
    MODEL_NOT_FOUND = "context_model_not_found"
    SUPERSEDED = "context_refresh_superseded"
    NOT_LOCAL = "context_not_local"


_FATAL_DIAGNOSTICS = frozenset(ContextDiagnostic) - {ContextDiagnostic.LIMIT_CONFLICT}
_NATIVE_CANONICAL_PROVIDERS = frozenset({"lmstudio", "ollama"})


def positive_limit(value: object) -> int | None:
    """Accept only positive int32 integers or ASCII decimal strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not value or not value.isascii() or not value.isdecimal():
            return None
        digits = value.lstrip("0")
        if not digits or len(digits) > 10:
            return None
        value = int(digits)
    return value if isinstance(value, int) and 0 < value <= 2_147_483_647 else None


def _identity(value: object, name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _provenance(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(path, str) or not key or not path
        for key, path in value.items()
    ):
        raise ValueError("provenance must map field names to nonempty source paths")
    return MappingProxyType(dict(value))


def _checked_limit(value: object, name: str) -> None:
    if value is not None and (type(value) is not int or positive_limit(value) is None):
        raise ValueError(f"{name} must be a positive int32 or None")


@dataclass(frozen=True, kw_only=True)
class LocalContextInstance:
    """One eligible serving instance, including its model/artifact identity."""

    model_id: str
    instance_id: str | None = None
    digest: str | None = None
    canonical_limit: int | None = None
    runtime_limit: int | None = None
    provenance: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[ContextDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        for name in ("model_id", "instance_id", "digest"):
            _identity(getattr(self, name), name, optional=name != "model_id")
        for name in ("canonical_limit", "runtime_limit"):
            _checked_limit(getattr(self, name), name)
        object.__setattr__(self, "provenance", _provenance(self.provenance))
        object.__setattr__(
            self, "diagnostics", tuple(ContextDiagnostic(d) for d in self.diagnostics)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "instance_id": self.instance_id,
            "digest": self.digest,
            "canonical_limit": self.canonical_limit,
            "runtime_limit": self.runtime_limit,
            "provenance": dict(self.provenance),
            "diagnostics": [d.value for d in self.diagnostics],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LocalContextInstance:
        _check_fields(data, cls.__dataclass_fields__)
        _check_diagnostics(data["diagnostics"])
        return cls(**data)


@dataclass(frozen=True, kw_only=True)
class LocalContextObservation:
    """Evidence for one route; summaries are derived, never caller assertions."""

    machine_id: str
    endpoint_id: str
    configuration_fingerprint: str
    provider: str
    model_id: str
    instance_id: str | None = None
    digest: str | None = None
    canonical_limit: int | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    provenance: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[ContextDiagnostic, ...] = ()
    instances: tuple[LocalContextInstance, ...] = ()
    runtime_limit: int | None = field(init=False)
    effective_limit: int | None = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "machine_id",
            "endpoint_id",
            "configuration_fingerprint",
            "provider",
            "model_id",
            "instance_id",
            "digest",
        ):
            _identity(getattr(self, name), name, optional=name in {"instance_id", "digest"})
        object.__setattr__(self, "provider", self.provider.strip().lower())
        _checked_limit(self.canonical_limit, "canonical_limit")
        if not isinstance(self.observed_at, datetime) or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be a timezone-aware datetime")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))
        object.__setattr__(self, "provenance", _provenance(self.provenance))
        instances = tuple(self.instances)
        if any(not isinstance(instance, LocalContextInstance) for instance in instances):
            raise ValueError("instances must contain LocalContextInstance evidence")
        object.__setattr__(self, "instances", instances)
        diagnostics = {ContextDiagnostic(d) for d in self.diagnostics}
        runtime, effective = _calculate(self, diagnostics)
        object.__setattr__(self, "runtime_limit", runtime)
        object.__setattr__(self, "effective_limit", effective)
        object.__setattr__(self, "diagnostics", tuple(sorted(diagnostics)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "endpoint_id": self.endpoint_id,
            "configuration_fingerprint": self.configuration_fingerprint,
            "provider": self.provider,
            "model_id": self.model_id,
            "instance_id": self.instance_id,
            "digest": self.digest,
            "canonical_limit": self.canonical_limit,
            "runtime_limit": self.runtime_limit,
            "effective_limit": self.effective_limit,
            "observed_at": self.observed_at.isoformat(),
            "provenance": dict(self.provenance),
            "diagnostics": [d.value for d in self.diagnostics],
            "instances": [instance.to_dict() for instance in self.instances],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LocalContextObservation:
        """Reject malformed or forged persisted summaries instead of trusting them."""
        _check_fields(data, cls.__dataclass_fields__)
        _check_diagnostics(data["diagnostics"])
        if not isinstance(data["observed_at"], str) or not isinstance(data["instances"], list):
            raise ValueError("invalid observation timestamp or instances")
        values = dict(data)
        runtime = values.pop("runtime_limit")
        effective = values.pop("effective_limit")
        _checked_limit(runtime, "runtime_limit")
        _checked_limit(effective, "effective_limit")
        values["observed_at"] = datetime.fromisoformat(values["observed_at"])
        values["instances"] = tuple(LocalContextInstance.from_dict(v) for v in values["instances"])
        observation = cls(**values)
        if (runtime, effective) != (observation.runtime_limit, observation.effective_limit):
            raise ValueError("observation summaries disagree with instance evidence")
        return observation


def _check_fields(data: Mapping[str, Any], fields: Mapping[str, object]) -> None:
    if not isinstance(data, Mapping) or set(data) != set(fields):
        raise ValueError("invalid local context evidence fields")


def _check_diagnostics(value: object) -> None:
    if not isinstance(value, list) or any(not isinstance(d, str) for d in value):
        raise ValueError("diagnostics must be an array of diagnostic strings")


def _calculate(
    observation: LocalContextObservation, diagnostics: set[ContextDiagnostic]
) -> tuple[int | None, int | None]:
    eligible = tuple(
        instance
        for instance in observation.instances
        if observation.instance_id is None or instance.instance_id == observation.instance_id
    )
    if not eligible:
        diagnostics.add(ContextDiagnostic.RUNTIME_UNKNOWN)
        if observation.instance_id is not None:
            diagnostics.add(ContextDiagnostic.INSTANCE_MISSING)
        return None, None

    limits: list[int] = []
    runtimes: list[int] = []
    digests = {i.digest for i in eligible if i.digest is not None}
    if observation.digest is not None:
        digests.add(observation.digest)
    if len(digests) > 1 or any(i.model_id != observation.model_id for i in eligible):
        diagnostics.add(ContextDiagnostic.IDENTITY_CONFLICT)
    canonical_limits = {i.canonical_limit for i in eligible if i.canonical_limit is not None}
    if observation.canonical_limit is not None:
        canonical_limits.add(observation.canonical_limit)
    if len(canonical_limits) > 1:
        diagnostics.add(ContextDiagnostic.LIMIT_CONFLICT)
    for instance in eligible:
        diagnostics.update(instance.diagnostics)
        canonical = instance.canonical_limit or observation.canonical_limit
        if canonical is None and observation.provider in _NATIVE_CANONICAL_PROVIDERS:
            diagnostics.add(ContextDiagnostic.CANONICAL_UNKNOWN)
        for value in (instance.canonical_limit, observation.canonical_limit):
            if value is not None:
                limits.append(value)
        if instance.runtime_limit is None:
            diagnostics.add(ContextDiagnostic.RUNTIME_UNKNOWN)
        else:
            runtimes.append(instance.runtime_limit)
            limits.append(instance.runtime_limit)
        if canonical is not None and instance.runtime_limit is not None:
            if instance.runtime_limit > canonical:
                diagnostics.add(ContextDiagnostic.LIMIT_CONFLICT)
    runtime = min(runtimes) if len(runtimes) == len(eligible) else None
    effective = None if diagnostics & _FATAL_DIAGNOSTICS else min(limits)
    return runtime, effective


def build_context_observation(
    *,
    machine_id: str,
    endpoint_id: str,
    configuration_fingerprint: str,
    provider: str,
    model_id: str,
    canonical_limit: object = None,
    instances: Iterable[LocalContextInstance] = (),
    instance_id: str | None = None,
    digest: str | None = None,
    observed_at: datetime | None = None,
    provenance: Mapping[str, str] | None = None,
    diagnostics: Iterable[ContextDiagnostic] = (),
) -> LocalContextObservation:
    """Normalize a collector's architecture value, preserving unusable evidence."""
    reasons = set(diagnostics)
    canonical = positive_limit(canonical_limit)
    if canonical_limit is not None and canonical is None:
        reasons.add(ContextDiagnostic.INVALID_METADATA)
    return LocalContextObservation(
        machine_id=machine_id,
        endpoint_id=endpoint_id,
        configuration_fingerprint=configuration_fingerprint,
        provider=provider,
        model_id=model_id,
        canonical_limit=canonical,
        instances=tuple(instances),
        instance_id=instance_id,
        digest=digest,
        observed_at=observed_at or datetime.now(UTC),
        provenance=provenance or {},
        diagnostics=tuple(reasons),
    )


def effective_local_limit(
    observation: LocalContextObservation | None, *overrides: object
) -> int | None:
    """Manual/reported caps may reduce verified evidence, never replace it."""
    if observation is None or observation.effective_limit is None:
        return None
    caps = [value for raw in overrides if (value := positive_limit(raw)) is not None]
    return min(observation.effective_limit, *caps) if caps else observation.effective_limit
