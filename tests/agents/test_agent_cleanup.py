from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents import agent_cleanup
from gobby.agents.agent_cleanup import (
    AgentCleanupHandler,
    cleanup_merged_task_artifacts_after_agent_exit,
)
from gobby.storage.agents import AgentRun

pytestmark = pytest.mark.unit


def _run(task_id: str | None = "task-1") -> AgentRun:
    return AgentRun(
        id="run-1",
        parent_session_id="parent-1",
        child_session_id="child-1",
        provider="codex",
        prompt="test",
        status="success",
        created_at="2026-05-20T00:00:00+00:00",
        updated_at="2026-05-20T00:00:00+00:00",
        task_id=task_id,
        worktree_id="wt-1",
    )


def _handler(db: object, run_db=None) -> AgentCleanupHandler:
    async def default_run_db(func, *args, **kwargs):
        return func(*args, **kwargs)

    clearable = MagicMock()
    return AgentCleanupHandler(
        agent_run_manager=MagicMock(),
        db=db,
        get_session_manager=lambda: None,
        get_session_coordinator=lambda: None,
        clone_storage=None,
        completion_registry=None,
        task_recovery=MagicMock(),
        prompt_detector=clearable,
        terminal_prompt_monitor=clearable,
        stall_classifier=clearable,
        loop_tracker=clearable,
        master_fds={},
        run_db=run_db or default_run_db,
    )


def test_cleanup_merged_task_artifacts_skips_when_merge_stage_not_done() -> None:
    db = MagicMock()
    task_manager = MagicMock()
    task_manager.stage_states.get.return_value = SimpleNamespace(state="in_progress")
    task_manager.get_task.return_value = SimpleNamespace(closed_at=None, closed_reason=None)

    with (
        patch("gobby.storage.tasks.LocalTaskManager", return_value=task_manager),
        patch("gobby.build.controls.cleanup_successful_merge_artifacts") as cleanup,
    ):
        result = cleanup_merged_task_artifacts_after_agent_exit(db, "task-1")

    assert result == []
    cleanup.assert_not_called()


def test_cleanup_merged_task_artifacts_runs_for_already_implemented_close() -> None:
    db = MagicMock()
    artifacts = [SimpleNamespace(deleted=True, deferred=False)]
    task_manager = MagicMock()
    task_manager.stage_states.get.return_value = SimpleNamespace(state="in_progress")
    task_manager.get_task.return_value = SimpleNamespace(
        closed_at="2026-05-20T00:00:00+00:00",
        closed_reason="already_implemented",
    )

    with (
        patch("gobby.storage.tasks.LocalTaskManager", return_value=task_manager),
        patch(
            "gobby.build.controls.cleanup_successful_merge_artifacts",
            return_value=artifacts,
        ) as cleanup,
    ):
        result = cleanup_merged_task_artifacts_after_agent_exit(db, "task-1")

    assert result == artifacts
    cleanup.assert_called_once_with(db, "task-1")


def test_cleanup_merged_task_artifacts_runs_when_merge_stage_done() -> None:
    db = MagicMock()
    artifacts = [SimpleNamespace(deleted=True, deferred=False)]
    task_manager = MagicMock()
    task_manager.stage_states.get.return_value = SimpleNamespace(state="done")

    with (
        patch("gobby.storage.tasks.LocalTaskManager", return_value=task_manager),
        patch(
            "gobby.build.controls.cleanup_successful_merge_artifacts",
            return_value=artifacts,
        ) as cleanup,
    ):
        result = cleanup_merged_task_artifacts_after_agent_exit(db, "task-1")

    assert result == artifacts
    cleanup.assert_called_once_with(db, "task-1")


@pytest.mark.asyncio
async def test_post_terminal_cleanup_retries_merge_artifact_cleanup_for_task_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = object()
    calls: list[tuple[object, str]] = []

    def retry_cleanup(cleanup_db: object, task_id: str) -> list[SimpleNamespace]:
        calls.append((cleanup_db, task_id))
        return [SimpleNamespace(deleted=True, deferred=False)]

    monkeypatch.setattr(
        agent_cleanup,
        "cleanup_merged_task_artifacts_after_agent_exit",
        retry_cleanup,
    )
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0),
    )

    await _handler(db).post_terminal_cleanup(_run())

    assert calls == [(db, "task-1")]


@pytest.mark.asyncio
async def test_post_terminal_cleanup_skips_merge_artifact_cleanup_without_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup = MagicMock(return_value=[])
    monkeypatch.setattr(
        agent_cleanup,
        "cleanup_merged_task_artifacts_after_agent_exit",
        cleanup,
    )
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0),
    )

    await _handler(object()).post_terminal_cleanup(_run(task_id=None))

    cleanup.assert_not_called()
