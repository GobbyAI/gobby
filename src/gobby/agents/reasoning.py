"""Reasoning helpers for spawned-agent execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from gobby.agents.provider_capabilities import provider_supports_terminal_reasoning
from gobby.providers.capabilities.models import SpeedMode
from gobby.providers.capabilities.resolve import (
    CapabilityResolver,
    SpeedResolution,
)
from gobby.providers.capabilities.resolve import (
    ReasoningStatus as CapabilityReasoningStatus,
)

AUTO_REASONING_EFFORT = "auto"
ReasoningStatus = Literal[
    "not_requested",
    "applied",
    "unverified",
    "unsupported_provider",
    "unsupported_model",
]


class _UnavailableCapabilityService:
    def get_provider_snapshot(self, provider: str) -> None:
        return None


class _UnavailableModelMetadataStore:
    def get_context_window(self, model: str) -> None:
        return None

    def get_model_metadata(self, model: str) -> None:
        return None


_fallback_resolver = CapabilityResolver(
    _UnavailableCapabilityService(),
    _UnavailableModelMetadataStore(),
)


def normalize_reasoning_effort(value: str | None) -> str | None:
    """Normalize UI/API reasoning input."""
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    return normalized


@dataclass(frozen=True)
class SpawnReasoningResolution:
    """Resolved spawned-agent reasoning metadata."""

    requested_effort: str | None
    effective_effort: str | None
    reasoning_required: bool
    status: ReasoningStatus
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_effort": self.requested_effort,
            "effective_effort": self.effective_effort,
            "required": self.reasoning_required,
            "status": self.status,
            "message": self.message,
        }


def _get_capability_resolver() -> CapabilityResolver:
    from gobby.app_context import get_app_context

    ctx = get_app_context()
    resolver = getattr(ctx, "provider_capability_resolver", None) if ctx else None
    return cast(CapabilityResolver, resolver) if resolver is not None else _fallback_resolver


def resolve_spawn_speed(
    *,
    provider: str,
    model: str | None,
    speed_mode: Literal["standard", "fast"],
) -> SpeedResolution:
    """Resolve a request-scoped speed route for spawned-terminal execution."""
    return _get_capability_resolver().resolve_route(
        provider,
        model or "",
        SpeedMode(speed_mode),
        surface="spawn-cli",
    )


def resolve_spawn_reasoning(
    *,
    provider: str,
    model: str | None,
    requested_effort: str | None,
    reasoning_required: bool | None,
) -> SpawnReasoningResolution:
    """Resolve a spawn-time reasoning request for a terminal agent."""
    normalized_request = normalize_reasoning_effort(requested_effort)
    if normalized_request is None:
        return SpawnReasoningResolution(
            requested_effort=None,
            effective_effort=None,
            reasoning_required=False,
            status="not_requested",
        )

    required = bool(reasoning_required)
    transport_supports_effort = provider_supports_terminal_reasoning(provider)
    resolution = _get_capability_resolver().resolve_reasoning(
        provider,
        model or "",
        normalized_request,
        transport_supports_effort=transport_supports_effort,
    )
    if resolution.status is CapabilityReasoningStatus.REJECTED:
        status: ReasoningStatus = (
            "unsupported_model" if transport_supports_effort else "unsupported_provider"
        )
        if not transport_supports_effort:
            message = (
                f"Requested reasoning '{normalized_request}' was not applied because "
                f"spawned-terminal reasoning is not wired for provider '{provider}'."
            )
        else:
            model_label = f" model '{model}'" if model else ""
            message = (
                f"Requested reasoning '{normalized_request}' is not supported for "
                f"{provider}{model_label}: {resolution.reason}."
            )
        return SpawnReasoningResolution(
            requested_effort=normalized_request,
            effective_effort=None,
            reasoning_required=required,
            status=status,
            message=message,
        )

    return SpawnReasoningResolution(
        requested_effort=normalized_request,
        effective_effort=resolution.effective_effort,
        reasoning_required=required,
        status=(
            "unverified" if resolution.status is CapabilityReasoningStatus.UNVERIFIED else "applied"
        ),
    )
