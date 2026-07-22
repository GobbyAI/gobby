"""Hook-event ingestion for durable verification receipts."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType
from gobby.hooks.normalization import _SHELL_TOOLS
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.verification_receipts import (
    VerificationOutcome,
    VerificationReceiptStore,
    VerificationReceiptWrite,
)
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _extract_shell_output_text,
    _shell_tool_outcome,
)

_NATIVE_EXECUTION_ID_KEYS = (
    "tool_use_id",
    "toolUseId",
    "tool_call_id",
    "toolCallId",
    "call_id",
    "callId",
    "item_id",
    "itemId",
    "id",
)
_SOURCE_EVENT_ID_KEYS = ("source_event_id", "event_id", "eventId", "delivery_id", "deliveryId")


def _string_value(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def ensure_verification_execution_identity(event: HookEvent) -> None:
    """Attach a stable native or source-event-derived execution identity."""
    if not event.data or event.data.get("tool_name") not in _SHELL_TOOLS:
        return
    if _string_value(event.data, ("verification_execution_id",)):
        return

    native_id = _string_value(event.data, _NATIVE_EXECUTION_ID_KEYS)
    source_event_id = _string_value(event.data, _SOURCE_EVENT_ID_KEYS) or _string_value(
        event.metadata, _SOURCE_EVENT_ID_KEYS
    )
    if source_event_id is None:
        identity = {
            "source": event.source.value,
            "session_id": event.session_id,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp.isoformat(),
            "tool_name": event.data.get("tool_name"),
            "cwd": event.cwd,
            "command": _extract_shell_command(event),
        }
        canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        source_event_id = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    execution_id = native_id
    if execution_id is None:
        execution_id = "synthetic:" + hashlib.sha256(source_event_id.encode("utf-8")).hexdigest()
    event.data["verification_source_event_id"] = source_event_id
    event.data["verification_execution_id"] = execution_id


def _extract_output(event: HookEvent) -> str:
    for field in ("tool_output", "tool_result", "tool_response", "contentItems"):
        output = _extract_shell_output_text(event.data.get(field))
        if output:
            return output
    return ""


def _project_id(db: HubDatabase, session_id: str, event: HookEvent) -> str | None:
    if event.project_id:
        return event.project_id
    row = db.fetchone("SELECT project_id FROM sessions WHERE id = %s", (session_id,))
    return str(row["project_id"]) if row and row.get("project_id") else None


def persist_verification_receipt(
    event: HookEvent,
    variables: dict[str, Any],
    session_id: str,
    *,
    db: HubDatabase,
) -> None:
    """Upsert every shell command, including provisional before-tool events."""
    if not event.data or event.data.get("tool_name") not in _SHELL_TOOLS:
        return
    command = _extract_shell_command(event)
    if not command:
        return

    ensure_verification_execution_identity(event)
    execution_id = _string_value(event.data, ("verification_execution_id",))
    source_event_id = _string_value(event.data, ("verification_source_event_id",))
    project_id = _project_id(db, session_id, event)
    if execution_id is None or source_event_id is None or project_id is None:
        return

    store = VerificationReceiptStore(db)
    active_task_ref = variables.get("active_task_id")
    task_id, attribution_source = store.resolve_attribution(
        project_id=project_id,
        session_id=session_id,
        active_task_ref=active_task_ref if isinstance(active_task_ref, str) else None,
    )

    is_terminal = event.event_type == HookEventType.AFTER_TOOL
    outcome = _shell_tool_outcome(event) if is_terminal else None
    normalized_outcome: VerificationOutcome = "provisional"
    if outcome is not None:
        if outcome.succeeded is True:
            normalized_outcome = "success"
        elif outcome.succeeded is False:
            normalized_outcome = "failure"
        else:
            normalized_outcome = "unknown"

    store.upsert(
        VerificationReceiptWrite(
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            provider=event.source.value,
            execution_id=execution_id,
            source_event_id=source_event_id,
            evidence_type="validation_command",
            command=command,
            cwd=event.cwd,
            normalized_outcome=normalized_outcome,
            outcome_provenance=outcome.provenance if outcome is not None else "before_tool",
            exit_code=outcome.exit_code if outcome is not None else None,
            started_at=event.timestamp,
            completed_at=event.timestamp if is_terminal else None,
            output=_extract_output(event) if is_terminal else None,
            details={"tool_name": event.data.get("tool_name")},
            attribution_source=attribution_source,
            attribution_actor=session_id if task_id else None,
            attributed_at=event.timestamp if task_id else None,
        )
    )
