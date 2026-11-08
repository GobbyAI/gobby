"""Telemetry helpers for lossy adapter response translation."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import TypedDict

from gobby.adapters.capabilities import (
    ContextChannel,
    HookCapability,
    present_unsupported_response_fields,
)
from gobby.config.features import ToolResultOffloadConfig
from gobby.hooks.context_limits import additional_context_limit_for
from gobby.hooks.events import ContextPart, HookResponse, SessionSource
from gobby.hooks.receipt_effects import record_worker_staging
from gobby.llm.sdk_utils import Carry, deliver_context
from gobby.storage.tool_results import ToolResultStore
from gobby.telemetry.instruments import inc_counter

logger = logging.getLogger(__name__)

# Session variable queueing the ordered [label, text] parts a channel still owes the
# model. Each delivery ships from the head; whatever does not fit waits here.
CARRIED_CONTEXT_VARIABLE = "_carried_context"


class AdditionalContextPersistKwargs(TypedDict):
    """Session and store kwargs that let a channel's waiting parts reach its next delivery."""

    session_id: str | None
    store: object | None


class AdapterDegradationKind(StrEnum):
    """Lossy adapter translation categories."""

    DROPPED_FIELD = "dropped_field"
    REROUTED_FIELD = "rerouted_field"
    CONTEXT_PAGED = "context_paged"
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


def tool_result_store_from_hook_manager(hook_manager: object | None) -> ToolResultStore | None:
    """Build the existing ToolResultStore from a hook manager's hub database."""
    if hook_manager is None:
        return None
    db = getattr(hook_manager, "_database", None)
    if db is None:
        db = getattr(hook_manager, "db", None)
    if db is None:
        session_manager = getattr(hook_manager, "_session_manager", None)
        db = getattr(session_manager, "db", None)
    if db is None:
        return None
    return ToolResultStore(db, ToolResultOffloadConfig())


def persist_kwargs_from_hook_response(
    response: HookResponse,
    hook_manager: object | None,
) -> AdditionalContextPersistKwargs:
    """Session and store kwargs for carrying a channel's waiting parts."""
    metadata = response.metadata or {}
    session_id = metadata.get("session_id")
    return {
        "session_id": session_id if isinstance(session_id, str) and session_id else None,
        "store": tool_result_store_from_hook_manager(hook_manager),
    }


def persist_kwargs_from_mapping(
    resp: Mapping[str, object] | None,
    *,
    store: object | None = None,
    session_id: str | None = None,
) -> AdditionalContextPersistKwargs:
    """Carry kwargs from a serialized HookResponse dict (chat SDK path)."""
    metadata = resp.get("metadata") if isinstance(resp, Mapping) else None
    if not isinstance(metadata, Mapping):
        metadata = {}
    mapped_session = metadata.get("session_id")
    return {
        "session_id": (
            mapped_session if isinstance(mapped_session, str) and mapped_session else session_id
        ),
        "store": store,
    }


def page_context_for_adapter(
    parts: Sequence[ContextPart],
    *,
    provider: SessionSource | str,
    hook_type: str | None,
    destination_channel: ContextChannel | str,
    event_logger: logging.Logger | None = None,
    session_id: str | None = None,
    store: object | None = None,
) -> str:
    """Join labeled context parts and fit them to the provider's ship limit, paging the rest.

    Nothing is dropped or cut. Parts that do not ship now wait in the session's queue for
    the channel and ship first, in order, on its next delivery. Pages of an oversized part
    ship consecutively. Channels with no delivery (``none``) queue nothing.
    """
    ship_limit = additional_context_limit_for(provider)
    variable = _carried_variable(destination_channel)
    stored = _carried_context(store, session_id, variable) if session_id and variable else None
    pending = stored or []
    # A fresh copy of a queued part is the same context; the queued copy ships in its place.
    pending_pairs = set(pending)
    queue = [*pending, *(part for part in parts if tuple(part) not in pending_pairs)]
    aggregate_len = len("\n\n".join(text for _, text in queue if text))
    delivery = deliver_context(
        queue,
        limit=ship_limit,
        carry=Carry.QUEUED if stored is not None else Carry.UNQUEUED,
    )
    if aggregate_len > ship_limit:
        record_adapter_degradation(
            provider=provider,
            hook_type=hook_type,
            kind=AdapterDegradationKind.CONTEXT_PAGED,
            response_field="context",
            destination_channel=destination_channel,
            detail=(
                f"aggregate_len={aggregate_len} limit={ship_limit} "
                f"paged_parts={len(delivery.remaining)}"
            ),
            event_logger=event_logger,
        )
    if stored is not None and session_id and variable and (pending or delivery.remaining):
        _stage_carried_context(session_id, variable, delivery.remaining)
    elif delivery.remaining:
        logger.error(
            "additionalContext queue unavailable; parts not carried labels=%s",
            [label for label, _ in delivery.remaining],
        )
    return delivery.text


def _carried_variable(destination_channel: ContextChannel | str) -> str | None:
    """Session variable queueing a channel's waiting parts; a channel with no delivery has none."""
    if destination_channel == ContextChannel.NONE:
        return None
    if destination_channel == ContextChannel.ADDITIONAL_CONTEXT:
        return CARRIED_CONTEXT_VARIABLE
    return f"{CARRIED_CONTEXT_VARIABLE}_{destination_channel}"


def _carried_context(
    store: object | None, session_id: str, variable: str
) -> list[ContextPart] | None:
    """Read a channel's waiting parts; ``None`` when the queue cannot be read."""
    db = getattr(store, "db", None)
    if db is None:
        return None
    from gobby.workflows.state_manager import SessionVariableManager

    try:
        raw = SessionVariableManager(db).get_variable_subset(session_id, (variable,)).get(variable)
    except Exception:
        logger.warning("Failed to read carried context for %s", session_id, exc_info=True)
        return None
    if not isinstance(raw, list):
        return []
    return [
        (item[0], item[1])
        for item in raw
        if isinstance(item, list | tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        and isinstance(item[1], str)
        and item[1]
    ]


def _stage_carried_context(session_id: str, variable: str, waiting: list[ContextPart]) -> None:
    """Stage, on receipt, the whole queue still waiting; an empty queue clears it."""
    record_worker_staging(
        {
            "session_id": session_id,
            "session_variables": {
                variable: [[label, text] for label, text in waiting] or None,
            },
        }
    )
