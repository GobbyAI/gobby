"""Stage-native delivery MCP tool coverage."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops

pytestmark = pytest.mark.unit


def _context() -> SimpleNamespace:
    executed: list[tuple[str, tuple[object, ...]]] = []
    db = SimpleNamespace(
        executed=executed,
        transaction=lambda: nullcontext(
            SimpleNamespace(execute=lambda sql, params: executed.append((sql, params)))
        ),
        fetchone=MagicMock(return_value=None),
    )
    stage_states = SimpleNamespace(
        get=MagicMock(return_value=SimpleNamespace(stage_name="pr", state="in_progress")),
        complete_stage=MagicMock(return_value=SimpleNamespace(stage_name="merge", state="done")),
        fail_stage=MagicMock(return_value=SimpleNamespace(stage_name="merge", state="ready")),
    )
    return SimpleNamespace(
        task_manager=SimpleNamespace(
            db=db,
            stage_states=stage_states,
            get_task=MagicMock(return_value=SimpleNamespace(id="task-1")),
            update_task=MagicMock(),
        ),
        resolve_session_id=lambda session_ref: session_ref,
    )


def test_record_pr_opened_persists_pr_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stage_ops,
        "stage_state_operation_view",
        lambda stage: {"stage_name": stage.stage_name, "state": stage.state},
    )
    ctx = _context()
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("record_pr_opened")
    assert tool is not None

    result = tool(task_id="#1", pr_url="https://example.test/pr/1", github_pr_number=12)

    assert result["ok"] is True
    assert "pr_url" in ctx.task_manager.db.executed[0][0]
    ctx.task_manager.update_task.assert_called_once_with("#1", github_pr_number=12)


def test_record_merge_result_records_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stage_ops,
        "stage_state_operation_view",
        lambda stage: {"stage_name": stage.stage_name, "state": stage.state},
    )
    ctx = _context()
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("record_merge_result")
    assert tool is not None

    assert tool(task_id="#1", merge_sha="abc123")["stage"]["state"] == "done"
    assert tool(task_id="#1", failure_reason="conflict")["stage"]["state"] == "ready"

    ctx.task_manager.stage_states.complete_stage.assert_called_once()
    ctx.task_manager.stage_states.fail_stage.assert_called_once()
