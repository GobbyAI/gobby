from __future__ import annotations

from datetime import timedelta

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import VerificationReceiptStore
from gobby.utils.datetime import utc_now
from gobby.workflows.verification_receipt_ingestion import (
    ensure_verification_execution_identity,
    persist_verification_receipt,
)


def _session(session_manager: SessionManager, project_id: str):
    return session_manager.register(
        external_id=f"receipt-ingestion-{project_id}",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
    )


def _event(
    *,
    event_type: HookEventType,
    source: SessionSource = SessionSource.CODEX,
    execution_id: str | None = None,
    timestamp_offset: int = 0,
    exit_code: int | None = None,
) -> HookEvent:
    data: dict[str, object] = {
        "tool_name": "exec_command",
        "tool_input": {"cmd": "echo ordinary-command"},
    }
    if execution_id is not None:
        data["call_id"] = execution_id
    if event_type == HookEventType.AFTER_TOOL:
        data["tool_output"] = {"output": "ordinary output"}
        if exit_code is not None:
            data["exit_code"] = exit_code
    return HookEvent(
        event_type=event_type,
        session_id="external-session",
        source=source,
        timestamp=utc_now() + timedelta(seconds=timestamp_offset),
        data=data,
        cwd="/repo",
    )


@pytest.mark.parametrize(
    "source",
    [
        SessionSource.CLAUDE,
        SessionSource.CODEX,
        SessionSource.DROID,
        SessionSource.GROK,
        SessionSource.QWEN,
    ],
)
def test_native_before_after_pair_is_one_terminal_receipt_for_each_provider(
    temp_db,
    session_manager,
    sample_project,
    source: SessionSource,
) -> None:
    session = _session(session_manager, sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        f"Receipt task {source.value}",
        claimed_by_session_id=session.id,
    )
    before = _event(
        event_type=HookEventType.BEFORE_TOOL,
        source=source,
        execution_id=f"native-{source.value}",
    )
    after = _event(
        event_type=HookEventType.AFTER_TOOL,
        source=source,
        execution_id=f"native-{source.value}",
        timestamp_offset=1,
        exit_code=0,
    )

    variables = {"active_task_id": task.id}
    persist_verification_receipt(
        before,
        variables,
        session.id,
        db=temp_db,
    )
    persist_verification_receipt(
        after,
        variables,
        session.id,
        db=temp_db,
    )

    receipts = VerificationReceiptStore(temp_db).list_for_task(sample_project["id"], task.id)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.provider == source.value
    assert receipt.execution_id == f"native-{source.value}"
    assert receipt.normalized_outcome == "success"
    assert receipt.command == "echo ordinary-command"
    assert receipt.output_first_4k == "ordinary output"
    assert receipt.attribution_source == "active_task"
    projection = variables["verification_evidence"][-1]
    assert projection["evidence_type"] == "receipt_projection"
    assert projection["task_id"] == task.id
    assert projection["outcome_counts"] == {"success": 1}
    assert projection["success"] is True


def test_fallback_identity_is_repeatable_per_event_but_distinguishes_repeated_commands() -> None:
    first = _event(event_type=HookEventType.BEFORE_TOOL)
    second = _event(event_type=HookEventType.BEFORE_TOOL, timestamp_offset=1)

    ensure_verification_execution_identity(first)
    first_identity = first.data["verification_execution_id"]
    ensure_verification_execution_identity(first)
    ensure_verification_execution_identity(second)

    assert first.data["verification_execution_id"] == first_identity
    assert second.data["verification_execution_id"] != first_identity
    assert first.data["verification_source_event_id"] != second.data["verification_source_event_id"]


def test_terminal_receipt_without_machine_outcome_is_unknown(
    temp_db,
    session_manager,
    sample_project,
) -> None:
    session = _session(session_manager, sample_project["id"])
    event = _event(
        event_type=HookEventType.AFTER_TOOL,
        execution_id="unknown-outcome",
    )

    persist_verification_receipt(event, {}, session.id, db=temp_db)

    receipts, total = VerificationReceiptStore(temp_db).list_page(
        project_id=sample_project["id"],
        session_id=session.id,
        scope="unassigned",
        task_id=None,
        limit=20,
        offset=0,
    )
    assert total == 1
    assert receipts[0].normalized_outcome == "unknown"
    assert receipts[0].task_id is None
