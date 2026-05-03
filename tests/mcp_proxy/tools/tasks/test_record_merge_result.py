"""Phase 4 red contracts for record_merge_result delivery artifacts."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    spec,
    stage_row,
    task_row,
)

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


def _real_context(temp_db) -> SimpleNamespace:
    return SimpleNamespace(
        task_manager=LocalTaskManager(temp_db),
        resolve_session_id=lambda session_ref: session_ref,
    )


def _artifact_row(temp_db, task_id: str) -> dict[str, object]:
    row = temp_db.fetchone(
        """
        SELECT merge_sha, merge_report_ref
        FROM task_delivery_campaigns
        WHERE task_id = ?
        """,
        (task_id,),
    )
    assert row is not None
    return dict(row)


def _merge_task_in_progress(
    temp_db,
    sample_project,
    *,
    max_work_attempts: int | None = None,
    parent_task_id: str | None = None,
):
    task = create_task(
        temp_db,
        sample_project,
        task_type="feature",
        parent_task_id=parent_task_id,
    )
    stage_kwargs = {}
    if max_work_attempts is not None:
        stage_kwargs["max_work_attempts"] = max_work_attempts
    initialize_manifest(temp_db, task.id, [spec("merge", 0, **stage_kwargs)])
    LocalTaskManager(temp_db).stage_states.start_stage(
        task.id,
        "merge",
        by_session_id="merge-agent",
    )
    return task


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
    assert "task_delivery_campaigns" in sql
    assert "merge_sha" in sql
    assert "merge_report_ref" in sql
    assert params[3:5] == ("abc123", "merge-report.md")
    assert result["stage"] == {"stage_name": "merge", "state": "done"}
    ctx.task_manager.stage_states.complete_stage.assert_called_once()
    args, kwargs = ctx.task_manager.stage_states.complete_stage.call_args
    assert args == ("task-1", "merge")
    assert kwargs["by_session_id"] is None
    assert kwargs["commit_sha"] == "abc123"
    ctx.task_manager.close_task.assert_not_called()


def test_success_closes_task_via_terminal_close(temp_db, sample_project) -> None:
    task = _merge_task_in_progress(temp_db, sample_project)

    result = _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        merge_sha="mergeabc123",
        report_ref="merge-report.md",
    )

    assert result["stage"]["state"] == "done"
    assert stage_row(temp_db, task.id, "merge")["state"] == "done"
    assert task_row(temp_db, task.id)["closed_at"] is not None


def test_success_close_uses_manifest_exhausted_reason_and_merge_sha(
    temp_db,
    sample_project,
) -> None:
    task = _merge_task_in_progress(temp_db, sample_project)

    _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        merge_sha="mergeabc123",
        report_ref="merge-report.md",
    )

    row = task_row(temp_db, task.id)
    assert row["closed_reason"] == "manifest_exhausted"
    assert row["closed_commit_sha"] == "mergeabc123"
    assert _artifact_row(temp_db, task.id) == {
        "merge_sha": "mergeabc123",
        "merge_report_ref": "merge-report.md",
    }


def test_success_close_uses_cascade_descendants_true(temp_db, sample_project) -> None:
    parent = _merge_task_in_progress(temp_db, sample_project)
    child = create_task(
        temp_db,
        sample_project,
        title="Child closed by merge cascade",
        task_type="feature",
        parent_task_id=parent.id,
    )

    _record_merge_result(_real_context(temp_db))(
        task_id=parent.id,
        merge_sha="mergecascade123",
        report_ref="merge-cascade-report.md",
    )

    parent_row = task_row(temp_db, parent.id)
    child_row = task_row(temp_db, child.id)
    assert parent_row["closed_at"] is not None
    assert parent_row["closed_reason"] == "manifest_exhausted"
    assert child_row["closed_at"] is not None
    assert child_row["closed_reason"] == "merged"
    assert child_row["closed_commit_sha"] == "mergecascade123"


def test_success_does_not_invoke_public_close_task(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()

    _record_merge_result(ctx)(
        task_id="task-1",
        merge_sha="abc123",
        report_ref="merge-report.md",
    )

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
    assert "task_delivery_campaigns" in sql
    assert "merge_report_ref" in sql
    assert "merge_sha" not in sql
    assert params[3] == "merge-failure.md"
    assert result["stage"] == {"stage_name": "merge", "state": "ready"}
    ctx.task_manager.stage_states.fail_stage.assert_called_once()
    args, kwargs = ctx.task_manager.stage_states.fail_stage.call_args
    assert args == ("task-1", "merge")
    assert kwargs["reason"] == "merge conflict"
    assert kwargs["by_session_id"] is None
    assert kwargs.get("needs_human", False) is False


def test_failure_path(temp_db, sample_project) -> None:
    under_cap = _merge_task_in_progress(temp_db, sample_project, max_work_attempts=2)

    result = _record_merge_result(_real_context(temp_db))(
        task_id=under_cap.id,
        failure_reason="merge conflict",
        report_ref="merge-failure.md",
    )

    row = stage_row(temp_db, under_cap.id, "merge")
    assert result["stage"]["state"] == "ready"
    assert row["state"] == "ready"
    assert row["work_attempt_count"] == 1
    under_cap_state = task_row(temp_db, under_cap.id)
    assert under_cap_state["closed_at"] is None
    assert under_cap_state["is_escalated"] == 0
    assert _artifact_row(temp_db, under_cap.id)["merge_report_ref"] == "merge-failure.md"

    over_cap = _merge_task_in_progress(temp_db, sample_project, max_work_attempts=1)

    _record_merge_result(_real_context(temp_db))(
        task_id=over_cap.id,
        failure_reason="merge conflict again",
        report_ref="merge-failure-final.md",
    )

    row = stage_row(temp_db, over_cap.id, "merge")
    over_cap_state = task_row(temp_db, over_cap.id)
    assert row["state"] == "ready"
    assert row["work_attempt_count"] == 1
    assert over_cap_state["is_escalated"] == 1
    assert over_cap_state["escalated_at"] is not None
