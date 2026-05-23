"""Telemetry helpers for lossy adapter response translation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum

from gobby.adapters.capabilities import (
    ContextChannel,
    HookCapability,
    present_unsupported_response_fields,
)
from gobby.hooks.events import HookResponse, SessionSource
from gobby.llm.sdk_utils import ADDITIONAL_CONTEXT_LIMIT, truncate_additional_context
from gobby.telemetry.instruments import inc_counter

logger = logging.getLogger(__name__)


class AdapterDegradationKind(StrEnum):
    """Lossy adapter translation categories."""

    DROPPED_FIELD = "dropped_field"
    REROUTED_FIELD = "rerouted_field"
    CONTEXT_TRUNCATED = "context_truncated"
    REASON_COMPACTED = "reason_compacted"
    EMPTY_BLOCK_SENTINEL = "empty_block_sentinel"
    GRACEFUL_ERROR = "graceful_error"


def _provider_value(provider: SessionSource | str) -> str:
    return provider.value if isinstance(provider, SessionSource) else provider


def record_adapter_degradation(
    *,
    provider: SessionSource | str,
    hook_type: str | None,
    kind: AdapterDegradationKind | str,
    response_field: str | None = None,
    destination_channel: ContextChannel | str | None = None,
    detail: str | None = None,
    event_logger: logging.Logger | None = None,
) -> None:
    """Record a lossy adapter translation event."""
    kind_value = kind.value if isinstance(kind, AdapterDegradationKind) else kind
    destination = (
        destination_channel.value
        if isinstance(destination_channel, ContextChannel)
        else destination_channel
    )
    attributes = {
        "provider": _provider_value(provider),
        "hook_type": hook_type or "unknown",
        "kind": kind_value,
        "response_field": response_field or "unknown",
        "destination_channel": destination or "none",
    }
    inc_counter("adapter_degradations_total", attributes=attributes)
    (event_logger or logger).debug(
        "adapter degradation kind=%s provider=%s hook_type=%s field=%s destination=%s detail=%s",
        kind_value,
        attributes["provider"],
        attributes["hook_type"],
        attributes["response_field"],
        attributes["destination_channel"],
        detail or "",
    )


def record_unsupported_response_fields(
    response: HookResponse,
    *,
    provider: SessionSource | str,
    hook_type: str | None,
    capability: HookCapability | None,
    event_logger: logging.Logger | None = None,
) -> tuple[str, ...]:
    """Record populated HookResponse fields dropped by a provider hook."""
    unsupported = present_unsupported_response_fields(response, capability)
    for field_name in unsupported:
        record_adapter_degradation(
            provider=provider,
            hook_type=hook_type,
            kind=AdapterDegradationKind.DROPPED_FIELD,
            response_field=field_name,
            destination_channel=ContextChannel.NONE,
            event_logger=event_logger,
        )
    return unsupported


def truncate_context_for_adapter(
    text: str,
    *,
    provider: SessionSource | str,
    hook_type: str | None,
    destination_channel: ContextChannel | str,
    contributor_sizes: Mapping[str, int] | None = None,
    event_logger: logging.Logger | None = None,
) -> str:
    """Truncate context and record telemetry if the adapter must shorten it."""
    if len(text) > ADDITIONAL_CONTEXT_LIMIT:
        record_adapter_degradation(
            provider=provider,
            hook_type=hook_type,
            kind=AdapterDegradationKind.CONTEXT_TRUNCATED,
            response_field="context",
            destination_channel=destination_channel,
            detail=f"aggregate_len={len(text)} limit={ADDITIONAL_CONTEXT_LIMIT}",
            event_logger=event_logger,
        )
    return truncate_additional_context(
        text,
        contributor_sizes=contributor_sizes,
        logger=event_logger,
    )
