"""Shared reasoning-effort resolution at AI adapter boundaries."""

from __future__ import annotations

from typing import Protocol, cast

from gobby.agents.provider_capabilities import provider_reasoning_flag
from gobby.agents.reasoning import AUTO_REASONING_EFFORT, normalize_reasoning_effort
from gobby.ai.registry import AIAdapterStyle, CapabilityBinding
from gobby.providers.capabilities.resolve import (
    CapabilityResolver,
    ReasoningResolution,
    ReasoningStatus,
)


class ReasoningEffortRejectedError(ValueError):
    """Raised when reasoning must fail before adapter execution."""


class ReasoningResolver(Protocol):
    def resolve_reasoning(
        self,
        provider: str,
        model: str,
        effort: str | None,
        *,
        transport_supports_effort: bool,
    ) -> ReasoningResolution: ...


def _app_resolver() -> ReasoningResolver | None:
    from gobby.app_context import get_app_context

    context = get_app_context()
    resolver = getattr(context, "provider_capability_resolver", None) if context else None
    return cast(CapabilityResolver, resolver) if resolver is not None else None


def resolve_binding_reasoning(
    *,
    binding: CapabilityBinding,
    model: str,
    requested_effort: str | None,
    resolver: ReasoningResolver | None = None,
) -> ReasoningResolution:
    """Resolve one binding's requested effort without leaking ``auto`` to transports."""
    normalized = normalize_reasoning_effort(requested_effort)
    if normalized is None:
        return ReasoningResolution(None, None, ReasoningStatus.VERIFIED, None)

    execution_provider = binding.metadata.get("execution_provider")
    reasoning_provider = (
        execution_provider
        if isinstance(execution_provider, str) and execution_provider
        else binding.provider
    )
    transport_supports_effort = (
        binding.adapter_style
        in {
            AIAdapterStyle.LLM_PROVIDER,
            AIAdapterStyle.LOCAL,
            AIAdapterStyle.OPENAI_COMPATIBLE,
        }
        or provider_reasoning_flag(reasoning_provider) is not None
    )
    if not transport_supports_effort and normalized == AUTO_REASONING_EFFORT:
        return ReasoningResolution(normalized, None, ReasoningStatus.UNVERIFIED, None)
    effective_resolver = resolver or _app_resolver()
    if effective_resolver is None:
        if not transport_supports_effort:
            return ReasoningResolution(
                normalized,
                None,
                ReasoningStatus.REJECTED,
                "transport does not support reasoning effort",
            )
        return ReasoningResolution(
            normalized,
            None if normalized == AUTO_REASONING_EFFORT else normalized,
            ReasoningStatus.UNVERIFIED,
            None,
        )
    return effective_resolver.resolve_reasoning(
        reasoning_provider,
        model,
        normalized,
        transport_supports_effort=transport_supports_effort,
    )


def apply_binding_reasoning(
    *,
    binding: CapabilityBinding,
    model: str,
    requested_effort: str | None,
    resolver: ReasoningResolver | None = None,
) -> str | None:
    """Return the concrete transport effort or raise for a rejected request."""
    resolution = resolve_binding_reasoning(
        binding=binding,
        model=model,
        requested_effort=requested_effort,
        resolver=resolver,
    )
    if resolution.status is ReasoningStatus.REJECTED:
        raise ReasoningEffortRejectedError(resolution.reason or "reasoning effort rejected")
    return resolution.effective_effort
