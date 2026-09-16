"""Typed resolution of provider model capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from gobby.config.ai import ModelMetadataAlias, model_metadata_alias_source_key
from gobby.llm.context_window_values import positive_context_window
from gobby.providers.capabilities.local_context import (
    LocalContextObservation,
    effective_local_limit,
    positive_limit,
)
from gobby.providers.capabilities.models import (
    ModelCapability,
    ProviderSnapshot,
    ReasoningSupport,
    SourceState,
)


class ContextSource(StrEnum):
    """Source selected for a resolved context limit."""

    CALLER_OVERRIDE = "caller_override"
    ROUTE_OVERRIDE = "route_override"
    LOCAL_OBSERVATION = "local_observation"
    PROVIDER_MATRIX = "provider_matrix"
    OPENROUTER = "openrouter"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContextResolution:
    """Resolved context limit with its winning source."""

    value: int | None
    source: ContextSource


class ReasoningStatus(StrEnum):
    """Verification state for a requested reasoning effort."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ReasoningResolution:
    """Typed pre-dispatch result for reasoning effort resolution."""

    requested_effort: str | None
    effective_effort: str | None
    status: ReasoningStatus
    reason: str | None


class _CapabilityStore(Protocol):
    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None: ...


class _ModelMetadataStore(Protocol):
    def get_context_window(self, model: str) -> int | None: ...

    def get_model_metadata(self, model: str) -> _ReasoningMetadata | None: ...


class LocalContextRoute(Protocol):
    """Minimum route contract required by synchronous local resolution."""

    @property
    def is_local(self) -> bool: ...

    def matches_observation(self, observation: LocalContextObservation) -> bool: ...


def resolve_local_context(
    local_route: LocalContextRoute | None,
    local_observation: LocalContextObservation | None,
    *,
    caller_override: int | None = None,
    route_override: int | None = None,
) -> ContextResolution | None:
    """Resolve a known local route, or return None for the remote path."""
    if local_route is None or not local_route.is_local:
        return None
    if local_observation is None or not local_route.matches_observation(local_observation):
        return ContextResolution(None, ContextSource.UNKNOWN)

    local_value = effective_local_limit(
        local_observation,
        caller_override,
        route_override,
    )
    if local_value is None:
        return ContextResolution(None, ContextSource.UNKNOWN)
    if positive_limit(caller_override) == local_value:
        return ContextResolution(local_value, ContextSource.CALLER_OVERRIDE)
    if positive_limit(route_override) == local_value:
        return ContextResolution(local_value, ContextSource.ROUTE_OVERRIDE)
    return ContextResolution(local_value, ContextSource.LOCAL_OBSERVATION)


class _ReasoningMetadata(Protocol):
    @property
    def reasoning_present(self) -> bool | None: ...

    @property
    def reasoning_supported_efforts(self) -> tuple[str, ...] | None: ...

    @property
    def reasoning_default_effort(self) -> str | None: ...

    @property
    def reasoning_default_enabled(self) -> bool | None: ...

    @property
    def reasoning_mandatory(self) -> bool | None: ...


class CapabilityResolver:
    """Resolve context, reasoning, and route facts from durable capability data."""

    def __init__(
        self,
        store: _CapabilityStore,
        model_metadata_store: _ModelMetadataStore,
        model_metadata_aliases: list[ModelMetadataAlias] | None = None,
    ) -> None:
        self._store = store
        self._model_metadata_store = model_metadata_store
        self._model_metadata_aliases = tuple(model_metadata_aliases or ())

    def resolve_context(
        self,
        provider: str,
        model: str,
        *,
        caller_override: int | None = None,
        route_override: int | None = None,
        local_route: LocalContextRoute | None = None,
        local_observation: LocalContextObservation | None = None,
    ) -> ContextResolution:
        """Resolve local evidence or use the remote matrix precedence order."""
        local_resolution = resolve_local_context(
            local_route,
            local_observation,
            caller_override=caller_override,
            route_override=route_override,
        )
        if local_resolution is not None:
            return local_resolution

        caller_value = positive_context_window(caller_override)
        if caller_value is not None:
            return ContextResolution(caller_value, ContextSource.CALLER_OVERRIDE)

        route_value = positive_context_window(route_override)
        if route_value is not None:
            return ContextResolution(route_value, ContextSource.ROUTE_OVERRIDE)

        capability = self.find_model(provider, model)
        matrix_value = positive_context_window(
            capability.context_length if capability is not None else None
        )
        if matrix_value is not None:
            return ContextResolution(matrix_value, ContextSource.PROVIDER_MATRIX)

        metadata_value = positive_context_window(
            self._model_metadata_store.get_context_window(model)
        )
        if metadata_value is not None:
            return ContextResolution(metadata_value, ContextSource.OPENROUTER)

        source_key = model_metadata_alias_source_key(provider, model)
        alias = next(
            (
                candidate
                for candidate in self._model_metadata_aliases
                if (candidate.provider, candidate.provider_model_id) == source_key
            ),
            None,
        )
        if alias is not None:
            alias_value = positive_context_window(
                self._model_metadata_store.get_context_window(alias.openrouter_model_id)
            )
            if alias_value is not None:
                return ContextResolution(alias_value, ContextSource.OPENROUTER)
        return ContextResolution(None, ContextSource.UNKNOWN)

    def resolve_reasoning(
        self,
        provider: str,
        model: str,
        effort: str | None,
        *,
        transport_supports_effort: bool,
    ) -> ReasoningResolution:
        """Resolve unset, automatic (omitted), and explicitly pinned reasoning efforts."""
        if effort is None:
            return ReasoningResolution(None, None, ReasoningStatus.VERIFIED, None)

        requested = effort.strip().lower()
        if not requested:
            return ReasoningResolution(None, None, ReasoningStatus.VERIFIED, None)
        if requested == "auto":
            # ``auto`` omits the effort entirely so the provider's own default
            # applies. Every transport and model accepts an omitted effort, so
            # there is nothing to look up or verify.
            return ReasoningResolution(requested, None, ReasoningStatus.VERIFIED, None)

        capability = self.find_model(provider, model)
        if capability is not None and capability.reasoning is ReasoningSupport.UNSUPPORTED:
            return self._reject_reasoning(requested, "model does not support reasoning effort")
        if not transport_supports_effort:
            return self._reject_reasoning(requested, "transport does not support reasoning effort")

        if capability is not None and capability.reasoning is ReasoningSupport.KNOWN:
            supported_efforts = capability.supported_efforts
            if supported_efforts is not None:
                if requested not in supported_efforts:
                    return self._reject_reasoning(
                        requested, f"unsupported reasoning effort: {requested}"
                    )
                return ReasoningResolution(requested, requested, ReasoningStatus.VERIFIED, None)
        return self._resolve_openrouter_pin(provider, model, requested)

    def _resolve_openrouter_pin(
        self, provider: str, model: str, requested: str
    ) -> ReasoningResolution:
        metadata = self._find_reasoning_metadata(provider, model)
        if metadata is None or metadata.reasoning_present is None:
            return ReasoningResolution(requested, requested, ReasoningStatus.UNVERIFIED, None)
        if metadata.reasoning_present is False:
            return self._reject_reasoning(requested, "model does not support reasoning effort")
        if requested == "none" and metadata.reasoning_mandatory is True:
            return self._reject_reasoning(requested, "model requires reasoning")

        supported_efforts = metadata.reasoning_supported_efforts
        if supported_efforts is not None and requested not in supported_efforts:
            return self._reject_reasoning(requested, f"unsupported reasoning effort: {requested}")
        return ReasoningResolution(requested, requested, ReasoningStatus.VERIFIED, None)

    def find_model(self, provider: str, model: str) -> ModelCapability | None:
        """Return the provider snapshot row for ``model``, if the catalog has one."""
        snapshot = self._store.get_provider_snapshot(provider)
        if snapshot is None:
            return None
        return next(
            (
                capability
                for capability in snapshot.models
                if model == capability.canonical_model or model in capability.aliases
            ),
            None,
        )

    def has_authoritative_provider_catalog(self, provider: str) -> bool:
        """Return whether a live-collected capability snapshot is loaded for ``provider``.

        Only a source that has actually answered proves the model list is complete
        enough to reject against. The bundled cold-start seed marks every source
        ``STALE``: it is a floor that keeps model selection working before the first
        collector run, not an inventory of what the provider serves. Treating it as
        authoritative rejects models the provider genuinely supports but that shipped
        after the seed was written. A refresh that never succeeded leaves ``STALE``
        (when seed rows exist) or ``ERROR`` (when none do), and neither knows what the
        provider serves either.

        Snapshots carrying no sources at all (local endpoint scans) stay authoritative:
        their model list comes from the running server, which is the best inventory
        available for those providers.
        """
        snapshot = self._store.get_provider_snapshot(provider)
        if snapshot is None:
            return False
        if not snapshot.sources:
            return True
        return any(source.state is SourceState.OK for source in snapshot.sources)

    def providers_for_model(self, model: str, providers: tuple[str, ...]) -> tuple[str, ...]:
        """Return providers in ``providers`` whose snapshot lists ``model``."""
        return tuple(name for name in providers if self.find_model(name, model) is not None)

    def _find_reasoning_metadata(self, provider: str, model: str) -> _ReasoningMetadata | None:
        metadata = self._model_metadata_store.get_model_metadata(model)
        if metadata is not None:
            return metadata
        source_key = model_metadata_alias_source_key(provider, model)
        alias = next(
            (
                candidate
                for candidate in self._model_metadata_aliases
                if (candidate.provider, candidate.provider_model_id) == source_key
            ),
            None,
        )
        if alias is None:
            return None
        return self._model_metadata_store.get_model_metadata(alias.openrouter_model_id)

    @staticmethod
    def _reject_reasoning(effort: str, reason: str) -> ReasoningResolution:
        return ReasoningResolution(effort, None, ReasoningStatus.REJECTED, reason)
