"""Phase 4 red contracts for record_merge_result delivery artifacts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.delivery import TaskDeliveryStateManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._stage_types import IllegalStageTransitionError
from gobby.utils.session_context import session_context_for_test
from tests.storage.tasks._stage_test_helpers import (
    create_task,
    initialize_manifest,
    spec,
    stage_row,
    task_row,
)

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _context() -> SimpleNamespace:
    executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(sql: str, params: tuple[object, ...]) -> SimpleNamespace | None:
        executed.append((sql, params))
        if "FROM task_delivery_units" in sql:
            return SimpleNamespace(fetchall=lambda: [])
        if "SELECT task_id, state, merge_strategy" not in sql:
            return None
        return SimpleNamespace(
            fetchone=lambda: {
                "task_id": params[0],
                "state": None,
                "merge_strategy": None,
                "structured_pr_verdict": None,
                "pr_report_ref": None,
                "merge_sha": None,
                "merge_report_ref": None,
                "last_error": None,
                "created_at": None,
                "updated_at": None,
            }
        )

    db = SimpleNamespace(
        executed=executed,
        transaction=lambda: nullcontext(SimpleNamespace(execute=execute)),
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


def _record_merge_result(ctx: SimpleNamespace | RegistryContext) -> Any:
    tool = stage_ops.create_stage_ops_registry(cast(RegistryContext, ctx)).get_tool(
        "record_merge_result"
    )
    assert tool is not None
    return tool


def _patch_stage_view(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        stage_ops,
        "stage_state_operation_view",
        lambda stage: {"stage_name": stage.stage_name, "state": stage.state},
    )


def _real_context(temp_db: HubDatabase) -> RegistryContext:
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=LocalTaskManager(temp_db),
            resolve_session_id=lambda session_ref: session_ref,
        ),
    )


def _real_context_with_github(temp_db: HubDatabase, github: Any) -> RegistryContext:
    return cast(
        RegistryContext,
        SimpleNamespace(
            task_manager=LocalTaskManager(temp_db),
            resolve_session_id=lambda session_ref: session_ref,
            mcp_manager=github,
        ),
    )


def _register_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    external_id: str,
    *,
    agent_depth: int = 0,
) -> str:
    return (
        SessionManager(temp_db)
        .register(
            external_id=external_id,
            machine_id="21000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
            title=external_id,
            agent_depth=agent_depth,
        )
        .id
    )


def _running_agent_run(
    temp_db: HubDatabase,
    *,
    parent_session_id: str,
    child_session_id: str,
    run_id: str,
    agent_name: str,
    task_id: str | None = None,
) -> str:
    runs = LocalAgentRunManager(temp_db)
    run = runs.create(
        parent_session_id=parent_session_id,
        child_session_id=child_session_id,
        provider="claude",
        prompt=f"{agent_name} work",
        agent_name=agent_name,
        task_id=task_id,
        run_id=run_id,
    )
    runs.start(run.id)
    return run.id


def _artifact_row(temp_db: HubDatabase, task_id: str) -> dict[str, object]:
    row = temp_db.fetchone(
        """
        SELECT merge_sha, merge_report_ref
        FROM task_delivery_campaigns
        WHERE task_id = %s
        """,
        (task_id,),
    )
    assert row is not None
    return dict(row)


def _merge_task_in_progress(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    *,
    max_work_attempts: int | None = None,
    parent_task_id: str | None = None,
) -> Any:
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


def test_success_forwards_artifacts_and_completes_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()
    cleanup_calls: list[tuple[object, str]] = []
    monkeypatch.setattr(
        stage_ops,
        "cleanup_successful_merge_artifacts",
        lambda db, task_id: cleanup_calls.append((db, task_id)),
    )

    result = _record_merge_result(ctx)(
        task_id="task-1",
        merge_sha="abc123",
        report_ref="merge-report.md",
    )

    assert all(
        "INSERT INTO task_delivery_campaigns" not in sql
        for sql, _params in ctx.task_manager.db.executed
    )
    assert result["stage"] == {"stage_name": "merge", "state": "done"}
    ctx.task_manager.stage_states.complete_stage.assert_called_once()
    args, kwargs = ctx.task_manager.stage_states.complete_stage.call_args
    assert args == ("task-1", "merge")
    assert kwargs["by_session_id"] is None
    assert kwargs["commit_sha"] == "abc123"
    assert kwargs["merge_report_ref"] == "merge-report.md"
    ctx.task_manager.close_task.assert_not_called()
    assert cleanup_calls == [(ctx.task_manager.db, "task-1")]


def test_success_transition_failure_does_not_write_merged_campaign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()
    ctx.task_manager.stage_states.complete_stage.side_effect = IllegalStageTransitionError(
        "merge",
        "needs_review",
        "complete_stage",
        "required",
    )
    delivery = Mock()
    delivery.get_state.return_value = {"campaign": {}}
    monkeypatch.setattr(stage_ops, "TaskDeliveryStateManager", Mock(return_value=delivery))
    release = Mock()
    monkeypatch.setattr(stage_ops, "_release_current_agent_dispatch_mutex", release)

    with pytest.raises(IllegalStageTransitionError):
        _record_merge_result(ctx)(
            task_id="task-1",
            merge_sha="merge-sha",
        )

    assert delivery.record_campaign.call_count == 0
    assert release.call_count == 0
    assert ctx.task_manager.stage_states.complete_stage.call_count == 1
    assert ctx.task_manager.stage_states.complete_stage.call_args.args == ("task-1", "merge")
    assert ctx.task_manager.stage_states.complete_stage.call_args.kwargs == {
        "by_session_id": None,
        "commit_sha": "merge-sha",
        "merge_report_ref": None,
    }


def test_success_closes_task_via_terminal_close(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
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
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
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
    count_row = temp_db.fetchone(
        "SELECT COUNT(*) AS campaign_count FROM task_delivery_campaigns WHERE task_id = %s",
        (task.id,),
    )
    assert count_row is not None
    assert count_row["campaign_count"] == 1


def test_complete_stage_then_record_merge_result_enriches_one_campaign(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = _merge_task_in_progress(temp_db, sample_project)
    LocalTaskManager(temp_db).stage_states.complete_stage(
        task.id,
        "merge",
        by_session_id="merge-agent",
        commit_sha="shared-merge-sha",
    )

    result = _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        merge_sha="shared-merge-sha",
        report_ref="enriched-report.md",
    )

    count_row = temp_db.fetchone(
        "SELECT COUNT(*) AS campaign_count FROM task_delivery_campaigns WHERE task_id = %s",
        (task.id,),
    )
    assert result["idempotent"] is True
    assert _artifact_row(temp_db, task.id) == {
        "merge_sha": "shared-merge-sha",
        "merge_report_ref": "enriched-report.md",
    }
    assert count_row is not None
    assert count_row["campaign_count"] == 1


def test_success_is_idempotent_after_worker_recorded_merge(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    task = _merge_task_in_progress(temp_db, sample_project)

    first = _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        merge_sha="merge-worker-sha",
        report_ref="merge-worker-report.md",
    )
    second = _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        merge_sha="merge-worker-sha",
        report_ref="merge-orchestrator-report.md",
    )

    assert first["stage"]["state"] == "done"
    assert second["ok"] is True
    assert second["idempotent"] is True
    assert second["stage"]["state"] == "done"
    assert stage_row(temp_db, task.id, "merge")["state"] == "done"
    assert task_row(temp_db, task.id)["closed_commit_sha"] == "merge-worker-sha"
    assert _artifact_row(temp_db, task.id) == {
        "merge_sha": "merge-worker-sha",
        "merge_report_ref": "merge-orchestrator-report.md",
    }


def test_success_idempotent_merge_rejects_different_completed_sha(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = _merge_task_in_progress(temp_db, sample_project)

    _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        merge_sha="merge-worker-sha",
        report_ref="merge-worker-report.md",
    )

    with pytest.raises(ValueError, match="different merge_sha"):
        _record_merge_result(_real_context(temp_db))(
            task_id=task.id,
            merge_sha="different-orchestrator-sha",
            report_ref="merge-orchestrator-report.md",
        )

    assert _artifact_row(temp_db, task.id) == {
        "merge_sha": "merge-worker-sha",
        "merge_report_ref": "merge-worker-report.md",
    }


def test_success_reconciles_ready_merge_after_prior_failure(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = _merge_task_in_progress(temp_db, sample_project, max_work_attempts=3)

    failed = _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        failure_reason="verification failed",
        report_ref="merge-failure.md",
    )
    result = _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        merge_sha="merge-retry-sha",
        report_ref="merge-retry-report.md",
    )

    assert failed["stage"]["state"] == "ready"
    assert result["ok"] is True
    assert result["reconciled"] is True
    assert result["stage"]["state"] == "done"
    row = stage_row(temp_db, task.id, "merge")
    assert row["state"] == "done"
    assert row["completed_commit_sha"] == "merge-retry-sha"
    task_state = task_row(temp_db, task.id)
    assert task_state["closed_commit_sha"] == "merge-retry-sha"
    assert task_state["is_escalated"] == 0
    assert _artifact_row(temp_db, task.id) == {
        "merge_sha": "merge-retry-sha",
        "merge_report_ref": "merge-retry-report.md",
    }


def test_success_reconciles_ready_merge_when_campaign_already_merged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = _merge_task_in_progress(temp_db, sample_project)
    manager = LocalTaskManager(temp_db)
    manager.stage_states.fail_stage(
        task.id,
        "merge",
        reason="stale failure already rolled stage back",
        by_session_id="merge-agent",
    )
    TaskDeliveryStateManager(temp_db).record_campaign(
        task.id,
        state="merged",
        merge_sha="already-merged-sha",
        merge_report_ref="previous-report.md",
        last_error="",
    )

    result = _record_merge_result(_real_context(temp_db))(
        task_id=task.id,
        merge_sha="already-merged-sha",
        report_ref="reconcile-report.md",
    )

    assert result["ok"] is True
    assert result["reconciled"] is True
    assert result["stage"]["state"] == "done"
    assert stage_row(temp_db, task.id, "merge")["completed_commit_sha"] == "already-merged-sha"
    assert task_row(temp_db, task.id)["closed_commit_sha"] == "already-merged-sha"
    assert _artifact_row(temp_db, task.id) == {
        "merge_sha": "already-merged-sha",
        "merge_report_ref": "reconcile-report.md",
    }


def test_success_rejects_different_ready_campaign_sha(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    task = _merge_task_in_progress(temp_db, sample_project)
    manager = LocalTaskManager(temp_db)
    manager.stage_states.fail_stage(
        task.id,
        "merge",
        reason="stale failure already rolled stage back",
        by_session_id="merge-agent",
    )
    TaskDeliveryStateManager(temp_db).record_campaign(
        task.id,
        state="merged",
        merge_sha="already-merged-sha",
        merge_report_ref="previous-report.md",
        last_error="",
    )

    with pytest.raises(ValueError, match="different merge_sha"):
        _record_merge_result(_real_context(temp_db))(
            task_id=task.id,
            merge_sha="different-sha",
            report_ref="different-report.md",
        )

    assert stage_row(temp_db, task.id, "merge")["state"] == "ready"
    assert task_row(temp_db, task.id)["closed_at"] is None
    assert _artifact_row(temp_db, task.id) == {
        "merge_sha": "already-merged-sha",
        "merge_report_ref": "previous-report.md",
    }


def test_success_releases_parent_merge_orchestrator_mutex_for_worker(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    root_session_id = _register_session(temp_db, sample_project, "root")
    orchestrator_session_id = _register_session(
        temp_db,
        sample_project,
        "merge-orchestrator-session",
        agent_depth=1,
    )
    worker_session_id = _register_session(
        temp_db,
        sample_project,
        "merge-worker-session",
        agent_depth=2,
    )
    task = _merge_task_in_progress(temp_db, sample_project)
    orchestrator_run_id = _running_agent_run(
        temp_db,
        parent_session_id=root_session_id,
        child_session_id=orchestrator_session_id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd4002",
        agent_name="merge-orchestrator",
        task_id=task.id,
    )
    _running_agent_run(
        temp_db,
        parent_session_id=orchestrator_session_id,
        child_session_id=worker_session_id,
        run_id="dddddddd-dddd-4ddd-8ddd-dddddddd400b",
        agent_name="merge-worker",
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="stage_dispatch",
        ttl_seconds=30,
        run_id=orchestrator_run_id,
    )

    with session_context_for_test(worker_session_id):
        result = _record_merge_result(_real_context(temp_db))(
            task_id=task.id,
            merge_sha="merge-child-recorded",
            report_ref="merge-worker-report.md",
        )

    assert result["stage"]["state"] == "done"
    assert stage_row(temp_db, task.id, "merge")["state"] == "done"
    assert task_row(temp_db, task.id)["closed_commit_sha"] == "merge-child-recorded"
    assert mutexes.get_mutex(task.id) is None


def test_success_close_uses_cascade_descendants_true(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    parent = _merge_task_in_progress(temp_db, sample_project)
    child = create_task(
        temp_db,
        sample_project,
        title="Child closed by merge cascade",
        task_type="feature",
        parent_task_id=parent.id,
    )
    closed_child = create_task(
        temp_db,
        sample_project,
        title="Child closed before merge cascade",
        task_type="feature",
        parent_task_id=parent.id,
    )
    temp_db.execute(
        """
        UPDATE tasks
           SET closed_at = %s,
               closed_reason = %s,
               closed_commit_sha = %s
         WHERE id = %s
        """,
        ("2026-01-02T03:04:05+00:00", "completed", "original-sha", closed_child.id),
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
    closed_child_row = task_row(temp_db, closed_child.id)
    assert closed_child_row["closed_at"].isoformat() == "2026-01-02T03:04:05+00:00"
    assert closed_child_row["closed_reason"] == "completed"
    assert closed_child_row["closed_commit_sha"] == "original-sha"


def test_success_does_not_invoke_public_close_task(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()

    _record_merge_result(ctx)(
        task_id="task-1",
        merge_sha="abc123",
        report_ref="merge-report.md",
    )

    ctx.task_manager.close_task.assert_not_called()
    assert ctx.task_manager.close_task.call_count == 0
    assert not ctx.task_manager.close_task.called


def test_failure_writes_report_and_fails_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stage_view(monkeypatch)
    ctx = _context()
    cleanup = Mock(side_effect=AssertionError("failure result must not cleanup worktrees"))
    monkeypatch.setattr(stage_ops, "cleanup_successful_merge_artifacts", cleanup)

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
    cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_close_linked_github_issue_tool_comments_labels_and_closes(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    class FakeGitHub:
        def __init__(self) -> None:
            from gobby.mcp_proxy.models import MCPServerConfig
            from gobby.storage.projects import GLOBAL_PROJECT_ID

            self.calls: list[tuple[str, dict[str, object]]] = []
            self.server_configs = [
                MCPServerConfig(
                    name="github",
                    project_id=GLOBAL_PROJECT_ID,
                    url="https://github-mcp.example.test",
                    id="github-global",
                    enabled=True,
                )
            ]

        async def call_tool(
            self,
            server_id: str,
            *,
            tool_name: str,
            arguments: dict[str, Any],
        ) -> dict[str, Any]:
            assert server_id == "github-global"
            self.calls.append((tool_name, arguments))
            return {}

    task = create_task(
        temp_db,
        sample_project,
        task_type="feature",
        github_repo="owner/repo",
        github_issue_number=11,
    )
    github = FakeGitHub()
    tool = stage_ops.create_stage_ops_registry(_real_context_with_github(temp_db, github)).get_tool(
        "close_linked_github_issue"
    )
    assert tool is not None

    result = await tool(task_id=task.id, merge_sha="abc123")

    assert result == {"ok": True, "task_id": task.id, "closed": True}
    assert [name for name, _args in github.calls] == [
        "add_labels_to_issue",
        "update_issue",
        "add_issue_comment",
    ]
    assert github.calls[0][1]["labels"] == ["gobby:resolved"]
    assert github.calls[1][1]["state"] == "closed"


def test_failure_path(temp_db: HubDatabase, sample_project: dict[str, Any]) -> None:
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
