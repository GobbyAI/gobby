"""Provider-agnostic ``tool_chat`` (agentic) generation service.

Peer of :class:`~gobby.ai._text_generation_service.TextGenerationService`.
Resolves a feature profile to ``TOOL_CHAT`` capability candidates, selects the
first available tool-capable binding, and dispatches **purely on the binding's
:class:`~gobby.ai.registry.AIAdapterStyle`** to a registered adapter. It holds
no provider names, no provider-specific model resolution, and no fallback to
another provider, to ``text_generate``, or to skeleton output — an exhausted
candidate list raises :class:`~gobby.ai.registry.CapabilityUnavailableError`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from typing import Protocol

from gobby.ai._reasoning import (
    ReasoningEffortRejectedError,
    ReasoningResolver,
    apply_binding_reasoning,
)
from gobby.ai._text_generation_helpers import _CandidateTimeoutError
from gobby.ai._tool_chat_contracts import (
    LIMIT_STOP_REASONS,
    ToolChatAdapter,
    ToolChatRequest,
    ToolChatResult,
    ToolLoopLimits,
)
from gobby.ai.endpoints import normalize_endpoint_routing
from gobby.ai.registry import (
    AIAdapterStyle,
    AICapability,
    AICapabilityRegistry,
    CapabilityBinding,
    CapabilityUnavailableError,
)
from gobby.config.feature_base import (
    DEFAULT_PROFILE_CANDIDATES,
    FeatureCandidateConfig,
    FeatureProfile,
    candidate_runtime_entries,
    parse_feature_candidate,
)
from gobby.providers.capabilities.apply import apply_speed, finalize_speed, speed_result
from gobby.providers.capabilities.models import SpeedMode
from gobby.providers.capabilities.resolve import SpeedResolution, SpeedStatus

logger = logging.getLogger(__name__)

ToolChatAdapterFactory = Callable[[], ToolChatAdapter]
ToolChatLeaseFactory = Callable[
    [ToolChatRequest, float],
    AbstractAsyncContextManager[ToolChatRequest],
]
_RUNTIME_ADAPTER_STYLES = frozenset(
    {
        AIAdapterStyle.LLM_PROVIDER,
        AIAdapterStyle.LOCAL,
        AIAdapterStyle.OPENAI_COMPATIBLE,
    }
)


class _CapabilityResolver(ReasoningResolver, Protocol):
    def resolve_route(
        self,
        provider: str,
        model: str,
        speed_mode: SpeedMode = SpeedMode.STANDARD,
        surface: str = "spawn-cli",
    ) -> SpeedResolution: ...


class ToolChatService:
    """Select a tool_chat binding by profile/candidate and dispatch by style.

    Adapter factories are keyed by :class:`AIAdapterStyle`, not by provider — the
    service never branches on a provider name. Selection iterates the resolved
    candidates and uses the first available tool-capable binding; if none is
    available the service raises rather than falling back to a different feature.
    """

    def __init__(
        self,
        registry: AICapabilityRegistry,
        *,
        adapters: Mapping[AIAdapterStyle, ToolChatAdapter] | None = None,
        adapter_factories: Mapping[AIAdapterStyle, ToolChatAdapterFactory] | None = None,
        profile_defaults: (
            Mapping[FeatureProfile, Sequence[str | FeatureCandidateConfig]] | None
        ) = None,
        default_limits: ToolLoopLimits | None = None,
        lease_factory: ToolChatLeaseFactory | None = None,
        capability_resolver: _CapabilityResolver | None = None,
    ) -> None:
        self._registry = registry
        self._adapters: dict[AIAdapterStyle, ToolChatAdapter] = dict(adapters or {})
        self._adapter_factories = dict(adapter_factories or {})
        self._default_limits = default_limits or ToolLoopLimits()
        self._lease_factory = lease_factory
        self._capability_resolver = capability_resolver
        self._profile_defaults = {
            FeatureProfile(profile): candidate_runtime_entries(candidates, profile=profile)
            for profile, candidates in (profile_defaults or {}).items()
        }

    @property
    def registry(self) -> AICapabilityRegistry:
        return self._registry

    async def chat_result(self, request: ToolChatRequest) -> ToolChatResult:
        """Run the first available tool_chat candidate; never fall back elsewhere."""
        limits = request.limits or self._default_limits
        request = replace(request, limits=limits)
        if self._lease_factory is None:
            return await self._chat_result_scoped(request, limits)
        async with self._lease_factory(
            request,
            limits.loop_timeout_seconds,
        ) as scoped_request:
            return await self._chat_result_scoped(
                replace(scoped_request, limits=limits),
                limits,
            )

    async def _chat_result_scoped(
        self,
        request: ToolChatRequest,
        limits: ToolLoopLimits,
    ) -> ToolChatResult:
        log_fields = _request_log_fields(request, limits)
        logger.debug("tool_chat request started", extra=log_fields)
        deadline = asyncio.get_running_loop().time() + limits.loop_timeout_seconds
        candidates = self._candidate_requests(request)
        attempted: list[str] = []
        errors: list[tuple[str, str]] = []
        unavailable_count = 0
        last_error: Exception | None = None
        for candidate in candidates:
            label = _candidate_label(candidate)
            candidate_log_fields = {
                **log_fields,
                "requested_provider": candidate.provider,
                "requested_model": candidate.model,
            }
            attempted.append(label)
            try:
                binding = self._select_binding(candidate)
                candidate = self._apply_candidate_reasoning(candidate, binding)
                adapter = self._adapter_for_style(binding.adapter_style)
                candidate, resolution = self._apply_candidate_speed(candidate, binding)
                result = await self._await_chat_candidate(
                    adapter,
                    candidate,
                    binding,
                    deadline=deadline,
                )
                result = replace(
                    result,
                    provider=result.provider or binding.provider,
                    model=(result.model or candidate.model or next(iter(binding.models), None)),
                    adapter_style=binding.adapter_style.value,
                )
                resolution = finalize_speed(
                    resolution,
                    {"model": result.model, **result.response_metadata},
                )
                result = replace(result, speed=speed_result(resolution))
                result_log_fields = _result_log_fields(candidate_log_fields, result)
                logger.debug("tool_chat request completed", extra=result_log_fields)
                if result.stop_reason in LIMIT_STOP_REASONS:
                    logger.info(
                        "tool_chat terminated by limit",
                        extra=result_log_fields,
                    )
                return result
            except CapabilityUnavailableError as exc:
                last_error = exc
                unavailable_count += 1
                errors.append((label, f"{type(exc).__name__}: {exc}"))
                logger.info(
                    "tool_chat capability unavailable",
                    extra={
                        **candidate_log_fields,
                        "provider": exc.provider or candidate.provider,
                        "model": exc.model or candidate.model,
                        "stop_reason": "capability_unavailable",
                        "failure_type": type(exc).__name__,
                    },
                )
                continue

        if attempted and unavailable_count == len(attempted):
            details = "; ".join(f"{candidate}: {error}" for candidate, error in errors)
            raise CapabilityUnavailableError(
                AICapability.TOOL_CHAT,
                reason=f"All tool_chat candidates unavailable: {details}",
            ) from last_error
        if len(attempted) == 1 and last_error is not None:
            raise last_error
        raise CapabilityUnavailableError(
            AICapability.TOOL_CHAT,
            reason=(f"No tool_chat candidate succeeded (tried: {attempted}; errors: {errors})"),
        ) from last_error

    async def _await_chat_candidate(
        self,
        adapter: ToolChatAdapter,
        request: ToolChatRequest,
        binding: CapabilityBinding,
        *,
        deadline: float,
    ) -> ToolChatResult:
        """Bound every candidate by the request's single shared loop deadline."""
        limits = request.limits
        if limits is None:
            raise RuntimeError("tool_chat limits must be resolved before adapter dispatch")
        timeout = deadline - asyncio.get_running_loop().time()
        if timeout <= 0:
            raise _CandidateTimeoutError("tool_chat request deadline expired")
        try:
            return await asyncio.wait_for(adapter.chat(request, binding), timeout=timeout)
        except TimeoutError as exc:
            raise _CandidateTimeoutError(
                f"tool_chat request timed out after {limits.loop_timeout_seconds:g}s"
            ) from exc

    def _candidate_requests(self, request: ToolChatRequest) -> tuple[ToolChatRequest, ...]:
        if request.candidates:
            return tuple(
                _candidate_request(request, candidate)
                for candidate in candidate_runtime_entries(
                    request.candidates, profile=request.profile
                )
            )
        provider, model = normalize_endpoint_routing(request.provider, request.model)
        if provider != request.provider or model != request.model:
            request = replace(request, provider=provider, model=model)
        has_provider = request.provider is not None
        has_model = request.model is not None
        if has_provider != has_model:
            raise ValueError(
                "provider and model must be supplied together for explicit tool_chat routing"
            )
        if has_provider and has_model:
            return (request,)
        if request.profile:
            profile = FeatureProfile(request.profile)
            candidates = self._profile_defaults.get(profile)
            if candidates is None:
                candidates = DEFAULT_PROFILE_CANDIDATES[profile]
            return tuple(
                _candidate_request(request, candidate)
                for candidate in candidate_runtime_entries(candidates, profile=profile)
            )
        return (request,)

    def _select_binding(self, request: ToolChatRequest) -> CapabilityBinding:
        binding = self._registry.select(
            AICapability.TOOL_CHAT,
            provider=request.provider,
            model=request.model,
        )
        allowed_styles = request.allowed_adapter_styles
        if allowed_styles is not None and binding.adapter_style not in allowed_styles:
            raise CapabilityUnavailableError(
                AICapability.TOOL_CHAT,
                provider=binding.provider,
                model=request.model,
                reason=(
                    f"Adapter style '{binding.adapter_style.value}' is disallowed for this request."
                ),
            )
        if request.builtins and binding.adapter_style not in _RUNTIME_ADAPTER_STYLES:
            raise CapabilityUnavailableError(
                AICapability.TOOL_CHAT,
                provider=binding.provider,
                model=request.model,
                reason=(
                    f"Adapter style '{binding.adapter_style.value}' cannot execute builtin tools."
                ),
            )
        return binding

    def _apply_candidate_reasoning(
        self,
        request: ToolChatRequest,
        binding: CapabilityBinding,
    ) -> ToolChatRequest:
        model = request.model or next(iter(binding.models), "")
        try:
            effort = apply_binding_reasoning(
                binding=binding,
                model=model,
                requested_effort=request.reasoning_effort,
                resolver=self._capability_resolver,
            )
        except ReasoningEffortRejectedError as exc:
            raise CapabilityUnavailableError(
                AICapability.TOOL_CHAT,
                provider=binding.provider,
                model=model,
                reason=str(exc),
            ) from exc
        return replace(request, reasoning_effort=effort)

    def _apply_candidate_speed(
        self,
        request: ToolChatRequest,
        binding: CapabilityBinding,
    ) -> tuple[ToolChatRequest, SpeedResolution]:
        model = request.model or next(iter(binding.models), None)
        mode = SpeedMode(request.speed_mode)
        resolver = self._capability_resolver
        if resolver is None:
            from gobby.app_context import get_app_context

            context = get_app_context()
            resolver = (
                getattr(context, "provider_capability_resolver", None)
                if context is not None
                else None
            )
        if resolver is not None and model is not None:
            resolution = resolver.resolve_route(
                binding.provider,
                model,
                mode,
                "app-server" if binding.provider == "codex" else "tool-chat",
            )
        else:
            unavailable = mode is SpeedMode.FAST
            resolution = SpeedResolution(
                requested=mode,
                effective=SpeedMode.STANDARD,
                status=(SpeedStatus.FAST_UNAVAILABLE if unavailable else SpeedStatus.STANDARD),
                selector=model or "",
                activations=(),
                reason="provider capability resolver unavailable" if unavailable else None,
            )
        application = apply_speed(
            resolution,
            model=model,
            request_parameters=request.request_parameters,
        )
        return (
            replace(
                request,
                model=application.model,
                request_parameters=application.request_parameters,
            ),
            resolution,
        )

    def _adapter_for_style(self, adapter_style: AIAdapterStyle) -> ToolChatAdapter:
        adapter = self._adapters.get(adapter_style)
        if adapter is not None:
            return adapter
        factory = self._adapter_factories.get(adapter_style)
        if factory is None:
            raise CapabilityUnavailableError(
                AICapability.TOOL_CHAT,
                reason=(
                    f"No tool_chat adapter registered for adapter style '{adapter_style.value}'."
                ),
            )
        adapter = factory()
        self._adapters[adapter_style] = adapter
        return adapter


def _candidate_request(
    request: ToolChatRequest, candidate: FeatureCandidateConfig
) -> ToolChatRequest:
    provider, model = parse_feature_candidate(candidate.candidate)
    provider, model = normalize_endpoint_routing(provider, model)
    reasoning_effort = (
        request.reasoning_effort
        if request.reasoning_effort is not None
        else candidate.reasoning_effort
    )
    return replace(request, provider=provider, model=model, reasoning_effort=reasoning_effort)


def _candidate_label(request: ToolChatRequest) -> str:
    return f"{request.provider or '?'}/{request.model or '?'}"


def _request_log_fields(
    request: ToolChatRequest,
    limits: ToolLoopLimits,
) -> dict[str, object]:
    """Return safe structured fields shared by every tool-chat log record."""
    return {
        "caller": request.caller,
        "request_id": request.request_id,
        "profile": request.profile,
        "requested_provider": request.provider,
        "requested_model": request.model,
        "provider": None,
        "model": None,
        "adapter_style": None,
        "stop_reason": None,
        "max_turns": limits.max_turns,
        "max_tool_calls": limits.max_tool_calls,
        "max_bytes_per_tool_result": limits.max_bytes_per_tool_result,
        "tool_timeout_seconds": limits.tool_timeout_seconds,
        "loop_timeout_seconds": limits.loop_timeout_seconds,
        "turns": None,
        "tool_use_count": 0,
        "tools": {},
    }


def _result_log_fields(
    request_fields: Mapping[str, object],
    result: ToolChatResult,
) -> dict[str, object]:
    return {
        **request_fields,
        "provider": result.provider,
        "model": result.model,
        "adapter_style": result.adapter_style,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "tool_use_count": result.tool_use_count,
        "tools": dict(result.tools),
    }
