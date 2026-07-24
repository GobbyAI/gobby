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
from dataclasses import replace

from gobby.ai._text_generation_helpers import _CandidateTimeoutError
from gobby.ai._text_generation_service import _SPAWN_COLD_ADAPTER_STYLES
from gobby.ai._tool_chat_contracts import (
    ToolChatAdapter,
    ToolChatRequest,
    ToolChatResult,
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

logger = logging.getLogger(__name__)

ToolChatAdapterFactory = Callable[[], ToolChatAdapter]
_RUNTIME_ADAPTER_STYLES = frozenset(
    {
        AIAdapterStyle.LLM_PROVIDER,
        AIAdapterStyle.LOCAL,
        AIAdapterStyle.OPENAI_COMPATIBLE,
    }
)


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
        attempt_timeout_seconds: float | None = None,
    ) -> None:
        self._registry = registry
        self._adapters: dict[AIAdapterStyle, ToolChatAdapter] = dict(adapters or {})
        self._adapter_factories = dict(adapter_factories or {})
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._profile_defaults = {
            FeatureProfile(profile): candidate_runtime_entries(candidates, profile=profile)
            for profile, candidates in (profile_defaults or {}).items()
        }

    @property
    def registry(self) -> AICapabilityRegistry:
        return self._registry

    async def chat_result(self, request: ToolChatRequest) -> ToolChatResult:
        """Run the first available tool_chat candidate; never fall back elsewhere."""
        candidates = self._candidate_requests(request)
        attempted: list[str] = []
        errors: list[tuple[str, str]] = []
        unavailable_count = 0
        last_error: Exception | None = None
        for candidate in candidates:
            label = _candidate_label(candidate)
            attempted.append(label)
            try:
                binding = self._select_binding(candidate)
                adapter = self._adapter_for_style(binding.adapter_style)
                result = await self._await_chat_candidate(adapter, candidate, binding)
                return replace(
                    result,
                    provider=result.provider or binding.provider,
                    model=(result.model or candidate.model or next(iter(binding.models), None)),
                    adapter_style=binding.adapter_style.value,
                )
            except CapabilityUnavailableError as exc:
                last_error = exc
                unavailable_count += 1
                errors.append((label, f"{type(exc).__name__}: {exc}"))
                logger.info("tool_chat candidate %s unavailable: %s", label, exc)
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

    def _candidate_timeout_for_binding(
        self, request: ToolChatRequest, binding: CapabilityBinding | None
    ) -> float | None:
        """Select the timeout bounding one tool_chat candidate run.

        A tool_chat candidate is a full multi-turn agentic run, so it is bounded
        by the overall attempt budget (``ai.generation.timeout_seconds``) — the
        tight single-generation candidate budgets that ``TextGenerationService``
        uses for fast failover (#17710) starve legitimate multi-minute agentic
        investigations (#18285). Request-level overrides can only tighten the
        bound: spawn-cold lanes read ``cli_candidate_timeout_seconds``, fast API
        lanes read ``candidate_timeout_seconds``.
        """
        if binding is not None and binding.adapter_style in _SPAWN_COLD_ADAPTER_STYLES:
            if request.cli_candidate_timeout_seconds is not None:
                return request.cli_candidate_timeout_seconds
        elif request.candidate_timeout_seconds is not None:
            return request.candidate_timeout_seconds
        return self._attempt_timeout_seconds

    async def _await_chat_candidate(
        self,
        adapter: ToolChatAdapter,
        request: ToolChatRequest,
        binding: CapabilityBinding,
    ) -> ToolChatResult:
        """Bound one tool_chat candidate by its lane-appropriate timeout.

        Unbounded ``adapter.chat`` lets a stuck multi-turn agentic loop wedge the
        caller indefinitely (the one-shot ``/api/llm/generate`` path is already
        bounded). On timeout this candidate fails so ``chat_result`` moves to the
        next one; a single timed-out candidate surfaces the timeout error.
        """
        timeout = self._candidate_timeout_for_binding(request, binding)
        if timeout is None:
            return await adapter.chat(request, binding)
        try:
            return await asyncio.wait_for(adapter.chat(request, binding), timeout=timeout)
        except TimeoutError as exc:
            raise _CandidateTimeoutError(
                f"tool_chat candidate timed out after {timeout:g}s"
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
