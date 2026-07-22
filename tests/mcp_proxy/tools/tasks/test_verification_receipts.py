from __future__ import annotations

from typing import Any

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle import create_lifecycle_registry
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.verification_receipts import (
    VerificationReceiptStore,
    VerificationReceiptWrite,
)
from gobby.utils.datetime import utc_now
from gobby.utils.session_context import session_context_for_test


def _receipt_write(
    *,
    project_id: str,
    session_id: str,
    execution_id: str,
) -> VerificationReceiptWrite:
    now = utc_now()
    return VerificationReceiptWrite(
        project_id=project_id,
        session_id=session_id,
        task_id=None,
        provider="codex",
        execution_id=execution_id,
        source_event_id=f"event:{execution_id}",
        evidence_type="validation_command",
        command=f"echo {execution_id}",
        cwd="/repo",
        normalized_outcome="success",
        outcome_provenance="structured_exit_code",
        exit_code=0,
        started_at=now,
        completed_at=now,
        output="ok",
        attribution_source="unassigned",
    )


@pytest.mark.asyncio
async def test_receipt_tools_paginate_assign_and_isolate_current_project(
    temp_db,
    sample_project: dict[str, Any],
) -> None:
    session_manager = SessionManager(temp_db)
    session = session_manager.register(
        external_id="receipt-tool-session",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        sample_project["id"],
        "Assign receipts",
        claimed_by_session_id=session.id,
    )
    store = VerificationReceiptStore(temp_db)
    current_receipts = [
        store.upsert(
            _receipt_write(
                project_id=sample_project["id"],
                session_id=session.id,
                execution_id=f"current-{index}",
            )
        )
        for index in range(2)
    ]
    other_project = LocalProjectManager(temp_db).create(
        "receipt-tools-other-project",
        repo_path="/tmp/receipt-tools-other-project",
    )
    other_session = session_manager.register(
        external_id="receipt-tool-other-session",
        machine_id="machine-1",
        source="codex",
        project_id=other_project.id,
    )
    store.upsert(
        _receipt_write(
            project_id=other_project.id,
            session_id=other_session.id,
            execution_id="outside-current-project",
        )
    )
    registry = create_lifecycle_registry(RegistryContext(task_manager=task_manager))

    with session_context_for_test(session.id):
        first_page = await registry.call(
            "list_task_verification_receipts",
            {"scope": "unassigned", "limit": 1, "offset": 0},
        )
        assignment = await registry.call(
            "assign_verification_receipts",
            {"receipt_ids": [current_receipts[0].id], "task_id": task.id},
        )
        task_receipts = await registry.call(
            "list_task_verification_receipts",
            {"scope": "task", "task_id": task.id},
        )
        repeated_assignment = await registry.call(
            "assign_verification_receipts",
            {"receipt_ids": [current_receipts[0].id], "task_id": task.id},
        )

    assert first_page["success"] is True
    assert first_page["pagination"] == {
        "limit": 1,
        "offset": 0,
        "total": 2,
        "next_offset": 1,
    }
    assert first_page["receipts"][0]["project_id"] == sample_project["id"]
    assert assignment["success"] is True
    assert assignment["assigned_count"] == 1
    assert assignment["receipts"][0]["attribution_source"] == "manual_assignment"
    assert task_receipts["pagination"]["total"] == 1
    assert task_receipts["receipts"][0]["id"] == current_receipts[0].id
    assert repeated_assignment["success"] is False
    assert "already assigned" in repeated_assignment["error"]
