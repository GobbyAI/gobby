"""Stage-native delivery MCP tool coverage."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from tests.storage.tasks._stage_test_helpers import create_task

pytestmark = pytest.mark.unit


def _context(temp_db, sample_project) -> SimpleNamespace:
    task = create_task(temp_db, sample_project, task_type="feature")
    stage_states = SimpleNamespace(
        get=MagicMock(return_value=SimpleNamespace(stage_name="pr", state="in_progress")),
        complete_stage=MagicMock(return_value=SimpleNamespace(stage_name="merge", state="done")),
        fail_stage=MagicMock(return_value=SimpleNamespace(stage_name="merge", state="ready")),
    )
    return SimpleNamespace(
        task_manager=SimpleNamespace(
            db=temp_db,
            stage_states=stage_states,
            get_task=MagicMock(return_value=SimpleNamespace(id=task.id)),
            update_task=MagicMock(),
        ),
        task_id=task.id,
        resolve_session_id=lambda session_ref: session_ref,
    )


def test_record_pr_opened_persists_pr_metadata(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    monkeypatch.setattr(
        stage_ops,
        "stage_state_operation_view",
        lambda stage: {"stage_name": stage.stage_name, "state": stage.state},
    )
    ctx = _context(temp_db, sample_project)
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("record_pr_opened")
    assert tool is not None

    result = tool(
        task_id=ctx.task_id,
        pr_url="https://example.test/pr/1",
        github_pr_number=12,
    )

    assert result["ok"] is True
    row = temp_db.fetchone(
        """
        SELECT pr_url, github_pr_number
        FROM task_delivery_units
        WHERE task_id = ?
        """,
        (ctx.task_id,),
    )
    assert row is not None
    assert row["pr_url"] == "https://example.test/pr/1"
    assert row["github_pr_number"] == 12
    ctx.task_manager.update_task.assert_not_called()


def test_record_merge_result_records_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    monkeypatch.setattr(
        stage_ops,
        "stage_state_operation_view",
        lambda stage: {"stage_name": stage.stage_name, "state": stage.state},
    )
    ctx = _context(temp_db, sample_project)
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("record_merge_result")
    assert tool is not None

    assert tool(task_id=ctx.task_id, merge_sha="abc123")["stage"]["state"] == "done"
    assert tool(task_id=ctx.task_id, failure_reason="conflict")["stage"]["state"] == "ready"

    ctx.task_manager.stage_states.complete_stage.assert_called_once()
    ctx.task_manager.stage_states.fail_stage.assert_called_once()
