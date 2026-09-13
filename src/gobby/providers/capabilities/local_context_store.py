"""Endpoint-scoped persistence for local context observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from urllib.parse import urlsplit

from gobby.providers.capabilities.local_context import LocalContextObservation
from gobby.providers.capabilities.models import (
    FactProvenance,
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
)
from gobby.providers.capabilities.store import ProviderCapabilityStore
from gobby.utils.url_sanitize import sanitize_url

_LOCAL_PROVIDER_PREFIX = "__local_context__"
_CONTEXT_FACT = "context_length"
_SOURCE_KEY = "local-context"


def local_provider_namespace(
    machine_id: str,
    endpoint_id: str,
    configuration_fingerprint: str,
) -> str:
    """Return the reserved provider key for one endpoint configuration."""
    for name, value in (
        ("machine_id", machine_id),
        ("endpoint_id", endpoint_id),
        ("configuration_fingerprint", configuration_fingerprint),
    ):
        _require_nonempty(name, value)
    endpoint_scope = json.dumps(
        [endpoint_id, configuration_fingerprint],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(endpoint_scope).hexdigest()
    return f"{_LOCAL_PROVIDER_PREFIX}:{machine_id}:{digest}"


def is_local_provider_namespace(provider: str) -> bool:
    """Return whether a provider key belongs to reserved local context storage."""
    return provider.strip().casefold().startswith(f"{_LOCAL_PROVIDER_PREFIX}:")


class LocalContextStore:
    """Project local observations onto atomic provider capability snapshots."""

    def __init__(self, store: ProviderCapabilityStore) -> None:
        self._store = store

    def replace_endpoint_snapshot(
        self,
        *,
        machine_id: str,
        endpoint_id: str,
        configuration_fingerprint: str,
        base_url: str,
        observations: Iterable[LocalContextObservation],
    ) -> None:
        """Atomically replace every observation for one endpoint configuration."""
        provider = local_provider_namespace(
            machine_id,
            endpoint_id,
            configuration_fingerprint,
        )
        source_url = _sanitized_base_url(base_url)
        models: list[ModelCapability] = []
        canonical_identities: set[str] = set()
        for observation in observations:
            _validate_scope(
                observation,
                machine_id=machine_id,
                endpoint_id=endpoint_id,
                configuration_fingerprint=configuration_fingerprint,
            )
            canonical_identity = _canonical_identity(observation)
            if canonical_identity in canonical_identities:
                raise ValueError(f"duplicate local context identity: {canonical_identity}")
            canonical_identities.add(canonical_identity)
            models.append(_capability(canonical_identity, source_url, observation))

        self._store.replace_provider_snapshot(
            ProviderSnapshot(
                provider=provider,
                generation=0,
                models=tuple(models),
                sources=(),
            )
        )

    def get_endpoint_observations(
        self,
        *,
        machine_id: str,
        endpoint_id: str,
        configuration_fingerprint: str,
    ) -> tuple[LocalContextObservation, ...]:
        """Load all observations for one exact endpoint configuration."""
        provider = local_provider_namespace(
            machine_id,
            endpoint_id,
            configuration_fingerprint,
        )
        snapshot = self._store.get_provider_snapshot(provider)
        if snapshot is None:
            return ()

        observations: list[LocalContextObservation] = []
        for model in snapshot.models:
            provenance = model.provenance.get(_CONTEXT_FACT)
            observation = provenance.local_context if provenance is not None else None
            if observation is None:
                raise ValueError("local capability row is missing context observation evidence")
            _validate_scope(
                observation,
                machine_id=machine_id,
                endpoint_id=endpoint_id,
                configuration_fingerprint=configuration_fingerprint,
            )
            if model.canonical_model != _canonical_identity(observation):
                raise ValueError("local capability identity disagrees with observation evidence")
            observations.append(observation)
        return tuple(observations)

    def get_observation(
        self,
        *,
        machine_id: str,
        endpoint_id: str,
        configuration_fingerprint: str,
        model_id: str,
        instance_id: str | None = None,
        digest: str | None = None,
    ) -> LocalContextObservation | None:
        """Load one exact model/instance/digest observation."""
        for observation in self.get_endpoint_observations(
            machine_id=machine_id,
            endpoint_id=endpoint_id,
            configuration_fingerprint=configuration_fingerprint,
        ):
            if (
                observation.model_id == model_id
                and observation.instance_id == instance_id
                and observation.digest == digest
            ):
                return observation
        return None


def _capability(
    canonical_identity: str,
    source_url: str,
    observation: LocalContextObservation,
) -> ModelCapability:
    return ModelCapability(
        canonical_model=canonical_identity,
        display_name=observation.model_id,
        aliases=(),
        available=True,
        hidden=False,
        is_default=False,
        context_length=observation.effective_limit,
        max_output_tokens=None,
        reasoning=ReasoningSupport.UNKNOWN,
        supported_efforts=None,
        default_effort=None,
        latency_class=None,
        input_modalities=None,
        supports_tools=None,
        provenance={
            _CONTEXT_FACT: FactProvenance(
                source_key=_SOURCE_KEY,
                source_url=source_url,
                observed_at=observation.observed_at,
                local_context=observation,
            )
        },
    )


def _canonical_identity(observation: LocalContextObservation) -> str:
    return json.dumps(
        [observation.model_id, observation.instance_id, observation.digest],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validate_scope(
    observation: LocalContextObservation,
    *,
    machine_id: str,
    endpoint_id: str,
    configuration_fingerprint: str,
) -> None:
    if (
        observation.machine_id != machine_id
        or observation.endpoint_id != endpoint_id
        or observation.configuration_fingerprint != configuration_fingerprint
    ):
        raise ValueError("local context observation does not match endpoint scope")


def _sanitized_base_url(base_url: str) -> str:
    _require_nonempty("base_url", base_url)
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    return sanitize_url(base_url)


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
