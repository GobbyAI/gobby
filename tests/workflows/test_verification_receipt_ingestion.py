from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import VerificationReceiptStore
from gobby.utils.datetime import utc_now
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.verification_receipt_ingestion import (
    ensure_verification_execution_identity,
    ingest_verification_receipt,
)


def _session(
    session_manager: SessionManager,
    project_id: str,
    *,
    suffix: str = "root",
    parent_session_id: str | None = None,
) -> Session:
    return session_manager.register(
        external_id=f"receipt-ingestion-{project_id}-{suffix}",
        machine_id="machine-1",
        source="codex",
        project_id=project_id,
        parent_session_id=parent_session_id,
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
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    source: SessionSource,
) -> None:
    session = _session(session_manager, sample_project["id"])
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        f"Receipt task {source.value}",
        claimed_by_session_id=session.id,
        validation_criteria="The command exits successfully for the attributed task.",
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

    variable_manager = SessionVariableManager(temp_db)
    variable_manager.set_variable(session.id, "active_task_id", task.id)
    ingest_verification_receipt(
        before,
        session.id,
        db=temp_db,
    )
    result = ingest_verification_receipt(
        after,
        session.id,
        db=temp_db,
    )

    assert result is not None
    assert result.acknowledged is True
    assert result.task_id == task.id
    variables = variable_manager.get_variables(session.id)
    receipts = VerificationReceiptStore(temp_db).list_for_task(sample_project["id"], task.id)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.provider == source.value
    assert receipt.execution_id == f"native-{source.value}"
    assert receipt.normalized_outcome == "success"
    assert receipt.command == "echo ordinary-command"
    assert receipt.output_first_4k == "ordinary output"
    assert receipt.validation_epoch == 0
    assert receipt.attribution_source == "active_task"
    projection = variables["verification_evidence"][-1]
    assert projection["evidence_type"] == "receipt_projection"
    assert projection["task_id"] == task.id
    assert projection["outcome_counts"] == {"success": 1}
    assert projection["success"] is True


def test_explicit_task_attribution_accepts_claim_owner_ancestor(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    owner = _session(session_manager, sample_project["id"], suffix="owner")
    child = _session(
        session_manager,
        sample_project["id"],
        suffix="child",
        parent_session_id=owner.id,
    )
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        "Child receipt task",
        claimed_by_session_id=owner.id,
        validation_criteria="A child validation command is attributed to its owner's task.",
    )
    event = _event(
        event_type=HookEventType.AFTER_TOOL,
        execution_id="child-validation",
        exit_code=0,
    )
    event.task_id = task.id

    result = ingest_verification_receipt(event, child.id, db=temp_db)

    assert result is not None
    assert result.task_id == task.id
    assert result.attribution_source == "explicit_task"
    assert result.normalized_outcome == "success"
    assert result.receipt.session_id == child.id
    assert result.receipt.attribution_actor == child.id


def test_explicit_task_attribution_rejects_unrelated_session(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    owner = _session(session_manager, sample_project["id"], suffix="owner")
    unrelated = _session(session_manager, sample_project["id"], suffix="unrelated")
    task = LocalTaskManager(temp_db).create_task(
        sample_project["id"],
        "Unrelated receipt task",
        claimed_by_session_id=owner.id,
        validation_criteria="An unrelated session cannot attribute validation to this task.",
    )
    event = _event(
        event_type=HookEventType.AFTER_TOOL,
        execution_id="unrelated-validation",
        exit_code=0,
    )
    event.task_id = task.id

    result = ingest_verification_receipt(event, unrelated.id, db=temp_db)

    assert result is not None
    assert result.task_id is None
    assert result.attribution_source == "unassigned"
    assert result.normalized_outcome == "success"


def test_explicit_task_attribution_rejects_cross_project_lineage(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    owner = _session(session_manager, sample_project["id"], suffix="cross-project-owner")
    executor_project = LocalProjectManager(temp_db).create(
        name="receipt-executor-project",
        repo_path="/tmp/receipt-executor-project",
    )
    child = _session(
        session_manager,
        executor_project.id,
        suffix="cross-project-child",
        parent_session_id=owner.id,
    )
    task = LocalTaskManager(temp_db).create_task(
        executor_project.id,
        "Cross-project receipt task",
        claimed_by_session_id=owner.id,
        validation_criteria="Cross-project lineage cannot authorize validation attribution.",
    )
    event = _event(
        event_type=HookEventType.AFTER_TOOL,
        execution_id="cross-project-validation",
        exit_code=0,
    )
    event.task_id = task.id

    result = ingest_verification_receipt(event, child.id, db=temp_db)

    assert result is not None
    assert result.task_id is None
    assert result.attribution_source == "unassigned"
    assert result.normalized_outcome == "success"


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
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
) -> None:
    session = _session(session_manager, sample_project["id"])
    event = _event(
        event_type=HookEventType.AFTER_TOOL,
        execution_id="unknown-outcome",
    )

    result = ingest_verification_receipt(event, session.id, db=temp_db)

    assert result is not None
    assert result.acknowledged is True
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
