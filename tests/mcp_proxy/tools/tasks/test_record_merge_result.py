"""Phase 4 red contracts for record_merge_result delivery artifacts."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

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
    )
    stage_states = SimpleNamespace(
        complete_stage=Mock(return_value=SimpleNamespace(stage_name="merge", state="done")),
        fail_stage=Mock(return_value=SimpleNamespace(stage_name="merge", state="ready")),
    )
    return SimpleNamespace(
        task_manager=SimpleNamespace(
            db=db,
            stage_states=stage_states,
            get_task=Mock(return_value=SimpleNamespace(id="task-1")),
            close_task=Mock(
                side_effect=AssertionError("record_merge_result must not call close_task")
            ),
        ),
        resolve_session_id=lambda session_ref: session_ref,
    )


def _record_merge_result(ctx: SimpleNamespace):
    tool = stage_ops.create_stage_ops_registry(ctx).get_tool("record_merge_result")
    assert tool is not None
    return tool


def _patch_stage_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stage_ops,
        "stage_state_operation_view",
        lambda stage: {"stage_name": stage.stage_name, "state": stage.state},
    )


def test_success_writes_artifacts_and_completes_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()

    result = _record_merge_result(ctx)(
        task_id="task-1",
        merge_sha="abc123",
        report_ref="merge-report.md",
    )

    sql, params = ctx.task_manager.db.executed[0]
    assert "merge_commit_sha" in sql
    assert "merge_campaign_report" in sql
    assert params[1:3] == ("abc123", "merge-report.md")
    assert result["stage"] == {"stage_name": "merge", "state": "done"}
    ctx.task_manager.stage_states.complete_stage.assert_called_once()
    args, kwargs = ctx.task_manager.stage_states.complete_stage.call_args
    assert args == ("task-1", "merge")
    assert kwargs["by_session_id"] is None
    assert kwargs["commit_sha"] == "abc123"
    ctx.task_manager.close_task.assert_not_called()


def test_failure_writes_report_and_fails_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()

    result = _record_merge_result(ctx)(
        task_id="task-1",
        failure_reason="merge conflict",
        report_ref="merge-failure.md",
    )

    sql, params = ctx.task_manager.db.executed[0]
    assert "merge_campaign_report" in sql
    assert "merge_commit_sha" not in sql
    assert params[1] == "merge-failure.md"
    assert result["stage"] == {"stage_name": "merge", "state": "ready"}
    ctx.task_manager.stage_states.fail_stage.assert_called_once()
    args, kwargs = ctx.task_manager.stage_states.fail_stage.call_args
    assert args == ("task-1", "merge")
    assert kwargs["reason"] == "merge conflict"
    assert kwargs["by_session_id"] is None
    assert kwargs.get("needs_human", False) is False
