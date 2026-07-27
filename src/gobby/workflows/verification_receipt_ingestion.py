"""Hook-event ingestion for durable verification receipts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from gobby.hooks.events import HookEvent, HookEventType
from gobby.hooks.normalization import _SHELL_TOOLS
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.verification_receipts import (
    VerificationOutcome,
    VerificationReceipt,
    VerificationReceiptStore,
    VerificationReceiptWrite,
)
from gobby.tasks.verification_outcome_projection import (
    VerificationOutcomeProjection,
    project_verification_outcomes,
)
from gobby.workflows.observer_utils import (
    _extract_shell_command,
    _extract_shell_output_text,
    _shell_tool_outcome,
)
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.verification_evidence import (
    MAX_VERIFICATION_EVIDENCE_ITEMS,
    VERIFICATION_EVIDENCE_RECORDED_VARIABLE,
    VERIFICATION_EVIDENCE_TYPE_RECEIPT_PROJECTION,
    VERIFICATION_EVIDENCE_VARIABLE,
    append_verification_evidence,
    receipt_projection_evidence,
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


@dataclass(frozen=True)
class VerificationReceiptIngestionResult:
    """Acknowledgment that one receipt and any task projection are durable."""

    receipt: VerificationReceipt
    normalized_outcome: VerificationOutcome
    task_id: str | None
    attribution_source: str
    projection: VerificationOutcomeProjection | None
    replayed: bool
    acknowledged: bool = True


class VerificationReceiptIngestionError(RuntimeError):
    """Retryable failure while durably ingesting a terminal shell outcome."""

    def __init__(self, identity: str | None) -> None:
        super().__init__("verification receipt ingestion failed")
        self.identity = identity


def is_verification_receipt_candidate(event: HookEvent) -> bool:
    """Return whether this event represents a shell execution boundary."""
    return bool(
        event.event_type in (HookEventType.BEFORE_TOOL, HookEventType.AFTER_TOOL)
        and event.data
        and event.data.get("tool_name") in _SHELL_TOOLS
    )


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


def verification_receipt_identity(event: HookEvent) -> str | None:
    """Return the stable execution identity attached during normalization."""
    ensure_verification_execution_identity(event)
    return _string_value(event.data, ("verification_execution_id",))


def _extract_output(event: HookEvent) -> str:
    for field in ("tool_output", "tool_result", "tool_response", "contentItems"):
        output = _extract_shell_output_text(event.data.get(field))
        if output:
            return output
    return ""


def _receipt_details(event: HookEvent) -> dict[str, Any]:
    """Preserve structured audit diagnostics without treating them as verdicts."""
    details: dict[str, Any] = {"tool_name": event.data.get("tool_name")}
    tool_result = event.data.get("tool_result")
    if isinstance(tool_result, dict):
        unknown_reason = tool_result.get("unknown_reason")
        if isinstance(unknown_reason, str) and unknown_reason:
            details["unknown_reason"] = unknown_reason
    return details


def _project_id(db: HubDatabase, session_id: str, event: HookEvent) -> str | None:
    if event.project_id:
        return event.project_id
    row = db.fetchone("SELECT project_id FROM sessions WHERE id = %s", (session_id,))
    return str(row["project_id"]) if row and row.get("project_id") else None


def ingest_verification_receipt(
    event: HookEvent,
    session_id: str,
    *,
    db: HubDatabase,
) -> VerificationReceiptIngestionResult | None:
    """Durably upsert one shell receipt and its task-scoped readiness projection."""
    if not is_verification_receipt_candidate(event):
        return None
    command = _extract_shell_command(event)
    if not command:
        return None

    ensure_verification_execution_identity(event)
    execution_id = _string_value(event.data, ("verification_execution_id",))
    source_event_id = _string_value(event.data, ("verification_source_event_id",))
    project_id = _project_id(db, session_id, event)
    if execution_id is None or source_event_id is None or project_id is None:
        return None

    variable_manager = SessionVariableManager(db)
    variables = variable_manager.get_variables(session_id)
    store = VerificationReceiptStore(db)
    active_task_ref = variables.get("active_task_id")
    task_id, attribution_source = store.resolve_attribution(
        project_id=project_id,
        session_id=session_id,
        active_task_ref=active_task_ref if isinstance(active_task_ref, str) else None,
        explicit_task_ref=event.task_id,
    )
    validation_epoch: int | None = None
    if task_id is not None:
        task_row = db.fetchone(
            "SELECT validation_epoch FROM tasks WHERE id = %s AND project_id = %s",
            (task_id, project_id),
        )
        if task_row is not None:
            validation_epoch = int(task_row["validation_epoch"])

    is_terminal = (
        event.event_type == HookEventType.AFTER_TOOL
        and event.data.get("_verification_pending") is not True
    )
    outcome = _shell_tool_outcome(event) if is_terminal else None
    normalized_outcome: VerificationOutcome = "pending"
    if outcome is not None:
        if outcome.succeeded is True:
            normalized_outcome = "success"
        elif outcome.succeeded is False:
            normalized_outcome = "failure"
        else:
            normalized_outcome = "unknown"

    receipt = store.upsert(
        VerificationReceiptWrite(
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            provider=event.source.value,
            execution_id=execution_id,
            source_event_id=source_event_id,
            evidence_type="shell_command",
            command=command,
            cwd=event.cwd,
            normalized_outcome=normalized_outcome,
            outcome_provenance=outcome.provenance if outcome is not None else "before_tool",
            exit_code=outcome.exit_code if outcome is not None else None,
            started_at=event.timestamp,
            completed_at=event.timestamp if is_terminal else None,
            output=_extract_output(event) if is_terminal else None,
            validation_epoch=validation_epoch,
            details=_receipt_details(event),
            attribution_source=attribution_source,
            attribution_actor=session_id if task_id else None,
            attributed_at=event.timestamp if task_id else None,
        )
    )
    projection: VerificationOutcomeProjection | None = None
    if receipt.task_id is not None:
        projection = project_verification_outcomes(store.list_for_task(project_id, receipt.task_id))
        projection_item = append_verification_evidence(
            [],
            receipt_projection_evidence(projection, task_id=receipt.task_id),
            session_id=session_id,
        )[0]
        variable_manager.upsert_bounded_list_variable(
            session_id,
            VERIFICATION_EVIDENCE_VARIABLE,
            projection_item,
            identity={
                "evidence_type": VERIFICATION_EVIDENCE_TYPE_RECEIPT_PROJECTION,
                "task_id": receipt.task_id,
            },
            max_items=MAX_VERIFICATION_EVIDENCE_ITEMS,
            updates={VERIFICATION_EVIDENCE_RECORDED_VARIABLE: projection.ready},
        )

    return VerificationReceiptIngestionResult(
        receipt=receipt,
        normalized_outcome=receipt.normalized_outcome,
        task_id=receipt.task_id,
        attribution_source=receipt.attribution_source,
        projection=projection,
        replayed=receipt.created_at != receipt.updated_at,
    )
