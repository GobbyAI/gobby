"""Typed resolution of provider model capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from gobby.config.ai import ModelMetadataAlias, model_metadata_alias_source_key
from gobby.llm.context_window_values import positive_context_window
from gobby.providers.capabilities.models import (
    ActivationDescriptor,
    ModelCapability,
    ModelRoute,
    ProviderSnapshot,
    ReasoningSupport,
    SpeedMode,
)


class ContextSource(StrEnum):
    """Source selected for a resolved context limit."""

    CALLER_OVERRIDE = "caller_override"
    ROUTE_OVERRIDE = "route_override"
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


class SpeedStatus(StrEnum):
    """Resolution and provider-confirmation state for a speed request."""

    STANDARD = "standard"
    FAST_CONFIGURED = "fast_configured"
    FAST_APPLIED = "fast_applied"
    FAST_UNAVAILABLE = "fast_unavailable"
    FAST_DEGRADED = "fast_degraded"


@dataclass(frozen=True)
class SpeedResolution:
    """Typed pre-dispatch result for a provider model route."""

    requested: SpeedMode
    effective: SpeedMode
    status: SpeedStatus
    selector: str
    activations: tuple[ActivationDescriptor, ...]
    reason: str | None


class _CapabilityStore(Protocol):
    def get_provider_snapshot(self, provider: str) -> ProviderSnapshot | None: ...


class _ModelMetadataStore(Protocol):
    def get_context_window(self, model: str) -> int | None: ...

    def get_model_metadata(self, model: str) -> _ReasoningMetadata | None: ...


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
    ) -> ContextResolution:
        """Resolve a context limit using the matrix's fixed precedence order."""
        caller_value = positive_context_window(caller_override)
        if caller_value is not None:
            return ContextResolution(caller_value, ContextSource.CALLER_OVERRIDE)

        route_value = positive_context_window(route_override)
        if route_value is not None:
            return ContextResolution(route_value, ContextSource.ROUTE_OVERRIDE)

        capability = self._find_model(provider, model)
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
        """Resolve unset, automatic, and explicitly pinned reasoning efforts."""
        if effort is None:
            return ReasoningResolution(None, None, ReasoningStatus.VERIFIED, None)

        requested = effort.strip().lower()
        if not requested:
            return ReasoningResolution(None, None, ReasoningStatus.VERIFIED, None)

        capability = self._find_model(provider, model)
        if capability is not None and capability.reasoning is ReasoningSupport.UNSUPPORTED:
            if requested == "auto":
                return ReasoningResolution(requested, None, ReasoningStatus.VERIFIED, None)
            return self._reject_reasoning(requested, "model does not support reasoning effort")
        if not transport_supports_effort:
            return self._reject_reasoning(requested, "transport does not support reasoning effort")

        if requested == "auto":
            if (
                capability is not None
                and capability.reasoning is ReasoningSupport.KNOWN
                and capability.default_effort is not None
            ):
                default_effort = capability.default_effort.strip().lower()
                return ReasoningResolution(
                    requested,
                    None if default_effort == "none" else default_effort,
                    ReasoningStatus.VERIFIED,
                    None,
                )
            return self._resolve_openrouter_auto(provider, model)

        if capability is not None and capability.reasoning is ReasoningSupport.KNOWN:
            supported_efforts = capability.supported_efforts
            if supported_efforts is not None:
                if requested not in supported_efforts:
                    return self._reject_reasoning(
                        requested, f"unsupported reasoning effort: {requested}"
                    )
                return ReasoningResolution(requested, requested, ReasoningStatus.VERIFIED, None)
        return self._resolve_openrouter_pin(provider, model, requested)

    def _resolve_openrouter_auto(self, provider: str, model: str) -> ReasoningResolution:
        metadata = self._find_reasoning_metadata(provider, model)
        if metadata is None or metadata.reasoning_present is None:
            return ReasoningResolution("auto", None, ReasoningStatus.UNVERIFIED, None)
        if metadata.reasoning_present is False:
            return ReasoningResolution("auto", None, ReasoningStatus.VERIFIED, None)

        default_effort = metadata.reasoning_default_effort
        if default_effort is not None:
            default_effort = default_effort.strip().lower()
        if metadata.reasoning_mandatory is True:
            if default_effort and default_effort != "none":
                return ReasoningResolution("auto", default_effort, ReasoningStatus.VERIFIED, None)
            return ReasoningResolution("auto", None, ReasoningStatus.UNVERIFIED, None)
        if metadata.reasoning_default_enabled is False or default_effort == "none":
            return ReasoningResolution("auto", None, ReasoningStatus.VERIFIED, None)
        if default_effort:
            return ReasoningResolution("auto", default_effort, ReasoningStatus.VERIFIED, None)
        return ReasoningResolution("auto", None, ReasoningStatus.UNVERIFIED, None)

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

    def resolve_route(
        self,
        provider: str,
        model: str,
        speed_mode: SpeedMode = SpeedMode.STANDARD,
        surface: str = "spawn-cli",
    ) -> SpeedResolution:
        """Resolve an exact route for one model and execution surface."""
        capability = self._find_model(provider, model)
        standard_route = self._find_route(capability, SpeedMode.STANDARD)
        standard_selector = standard_route.selector if standard_route is not None else model

        if speed_mode is SpeedMode.STANDARD:
            return SpeedResolution(
                requested=SpeedMode.STANDARD,
                effective=SpeedMode.STANDARD,
                status=SpeedStatus.STANDARD,
                selector=standard_selector,
                activations=self._surface_activations(standard_route, surface),
                reason=None,
            )

        fast_route = self._find_route(capability, SpeedMode.FAST)
        if fast_route is None:
            return SpeedResolution(
                requested=SpeedMode.FAST,
                effective=SpeedMode.STANDARD,
                status=SpeedStatus.FAST_UNAVAILABLE,
                selector=standard_selector,
                activations=(),
                reason="model has no available fast route",
            )

        activations = self._surface_activations(fast_route, surface)
        if not activations:
            return SpeedResolution(
                requested=SpeedMode.FAST,
                effective=SpeedMode.STANDARD,
                status=SpeedStatus.FAST_UNAVAILABLE,
                selector=standard_selector,
                activations=(),
                reason=f"fast route is unavailable on surface: {surface}",
            )
        return SpeedResolution(
            requested=SpeedMode.FAST,
            effective=SpeedMode.FAST,
            status=SpeedStatus.FAST_CONFIGURED,
            selector=fast_route.selector,
            activations=activations,
            reason=None,
        )

    def _find_model(self, provider: str, model: str) -> ModelCapability | None:
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
    def _find_route(capability: ModelCapability | None, speed_mode: SpeedMode) -> ModelRoute | None:
        if capability is None or not capability.available:
            return None
        return next(
            (
                route
                for route in capability.routes
                if route.speed_mode is speed_mode and route.available
            ),
            None,
        )

    @staticmethod
    def _surface_activations(
        route: ModelRoute | None, surface: str
    ) -> tuple[ActivationDescriptor, ...]:
        if route is None:
            return ()
        return tuple(
            activation for activation in route.activations if activation.surface == surface
        )

    @staticmethod
    def _reject_reasoning(effort: str, reason: str) -> ReasoningResolution:
        return ReasoningResolution(effort, None, ReasoningStatus.REJECTED, reason)
