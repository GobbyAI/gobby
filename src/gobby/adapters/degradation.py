"""Telemetry helpers for lossy adapter response translation."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import TypedDict

from gobby.adapters.capabilities import (
    ContextChannel,
    HookCapability,
    present_unsupported_response_fields,
)
from gobby.config.features import ToolResultOffloadConfig
from gobby.hooks.context_limits import additional_context_limit_for
from gobby.hooks.events import HookResponse, SessionSource
from gobby.llm.sdk_utils import truncate_additional_context
from gobby.storage.tool_results import ToolResultStore
from gobby.telemetry.instruments import inc_counter

logger = logging.getLogger(__name__)


class AdditionalContextPersistKwargs(TypedDict):
    """Session/project/store kwargs for additionalContext overflow persist."""

    session_id: str | None
    project_id: str | None
    store: object | None


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
    """Session/project/store kwargs for additionalContext overflow persist."""
    metadata = response.metadata or {}
    session_id = metadata.get("session_id")
    project_id = metadata.get("project_id")
    return {
        "session_id": session_id if isinstance(session_id, str) and session_id else None,
        "project_id": project_id if isinstance(project_id, str) and project_id else None,
        "store": tool_result_store_from_hook_manager(hook_manager),
    }


def persist_kwargs_from_mapping(
    resp: Mapping[str, object] | None,
    *,
    store: object | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
) -> AdditionalContextPersistKwargs:
    """Persist kwargs from a serialized HookResponse dict (chat SDK path)."""
    metadata = resp.get("metadata") if isinstance(resp, Mapping) else None
    if not isinstance(metadata, Mapping):
        metadata = {}
    mapped_session = metadata.get("session_id")
    mapped_project = metadata.get("project_id")
    return {
        "session_id": (
            mapped_session if isinstance(mapped_session, str) and mapped_session else session_id
        ),
        "project_id": (
            mapped_project if isinstance(mapped_project, str) and mapped_project else project_id
        ),
        "store": store,
    }


def truncate_context_for_adapter(
    text: str,
    *,
    provider: SessionSource | str,
    hook_type: str | None,
    destination_channel: ContextChannel | str,
    contributor_sizes: Mapping[str, int] | None = None,
    event_logger: logging.Logger | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    store: object | None = None,
) -> str:
    """Bound context and record telemetry if the adapter must shorten it."""
    ship_limit = additional_context_limit_for(provider)
    if len(text) > ship_limit:
        record_adapter_degradation(
            provider=provider,
            hook_type=hook_type,
            kind=AdapterDegradationKind.CONTEXT_TRUNCATED,
            response_field="context",
            destination_channel=destination_channel,
            detail=f"aggregate_len={len(text)} limit={ship_limit}",
            event_logger=event_logger,
        )
    return truncate_additional_context(
        text,
        contributor_sizes=contributor_sizes,
        logger=event_logger,
        limit=ship_limit,
        session_id=session_id,
        project_id=project_id,
        store=store,
    )
