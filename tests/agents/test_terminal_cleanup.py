from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents import terminal_cleanup
from gobby.agents.terminal_cleanup import cleanup_merged_task_artifacts_after_agent_exit
from tests.agents.cleanup_test_support import (
    AcknowledgingCompletionRegistry,
    RecordingCompletionRegistry,
    RecordingDb,
    _FailingNotifyRegistry,
    _handler,
    _run,
    _stub_runtime_cleanup,
)

pytestmark = pytest.mark.unit


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
    assert len(result) == 0
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
    assert result[0].deleted is True
    cleanup.assert_called_once_with(db, "task-1")


def test_cleanup_merged_task_artifacts_preserves_reused_worktree() -> None:
    db = MagicMock()
    artifacts = [SimpleNamespace(deleted=False, deferred=True)]
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
        result = cleanup_merged_task_artifacts_after_agent_exit(
            db,
            "task-1",
            preserve_worktree_id="wt-1",
        )

    assert result == artifacts
    assert result[0].deferred is True
    cleanup.assert_called_once_with(db, "task-1", preserve_worktree_ids={"wt-1"})


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
    assert result[0].deferred is False
    cleanup.assert_called_once_with(db, "task-1")


async def test_post_terminal_cleanup_retries_merge_artifact_cleanup_for_task_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    calls: list[tuple[object, str]] = []

    def retry_cleanup(cleanup_db: object, task_id: str) -> list[SimpleNamespace]:
        calls.append((cleanup_db, task_id))
        return [SimpleNamespace(deleted=True, deferred=False)]

    monkeypatch.setattr(
        terminal_cleanup,
        "cleanup_merged_task_artifacts_after_agent_exit",
        retry_cleanup,
    )
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0),
    )

    await _handler(db).post_terminal_cleanup(_run(), allow_parent_session_fallback=False)

    assert calls == [(db, "task-1")]
    assert db.executed == []


async def test_post_terminal_cleanup_preserves_reused_worktree_after_no_commit_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    calls: list[tuple[object, str, str | None]] = []

    def retry_cleanup(
        cleanup_db: object,
        task_id: str,
        *,
        preserve_worktree_id: str | None = None,
    ) -> list[SimpleNamespace]:
        calls.append((cleanup_db, task_id, preserve_worktree_id))
        return [SimpleNamespace(deleted=False, deferred=True)]

    monkeypatch.setattr(
        terminal_cleanup,
        "cleanup_merged_task_artifacts_after_agent_exit",
        retry_cleanup,
    )
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0),
    )

    await _handler(db).post_terminal_cleanup(
        _run(reused_worktree=True),
        allow_parent_session_fallback=False,
    )

    assert calls == [(db, "task-1", "wt-1")]


async def test_post_terminal_cleanup_skips_merge_artifact_cleanup_without_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    cleanup = MagicMock(return_value=[])
    monkeypatch.setattr(
        terminal_cleanup,
        "cleanup_merged_task_artifacts_after_agent_exit",
        cleanup,
    )
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0),
    )

    await _handler(db).post_terminal_cleanup(
        _run(task_id=None), allow_parent_session_fallback=False
    )

    cleanup.assert_not_called()
    assert db.executed == []


async def test_post_terminal_cleanup_preserves_registry_without_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    registry = RecordingCompletionRegistry()
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0),
    )

    await _handler(db, completion_registry=registry).post_terminal_cleanup(
        _run(task_id=None), allow_parent_session_fallback=False
    )

    assert registry.cleaned == []
    assert db.executed == []


async def test_daemon_stop_parking_skips_later_resource_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    session_manager = MagicMock()
    session_coordinator = MagicMock()
    runtime_calls: list[tuple[object, str, str | None, str | None]] = []

    def fail_subscriber_cleanup(**_kwargs: object) -> None:
        raise RuntimeError("subscriber cleanup failed")

    def cleanup_runtime_state(
        cleanup_db: object,
        *,
        run_id: str,
        child_session_id: str | None,
        terminal_reason: str | None,
    ) -> SimpleNamespace:
        runtime_calls.append((cleanup_db, run_id, child_session_id, terminal_reason))
        return SimpleNamespace(dispatch_mutex_rows=1, workflow_instance_rows=0)

    monkeypatch.setattr(
        "gobby.agents.completion_subscribers.remove_agent_completion_subscribers",
        fail_subscriber_cleanup,
    )
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        cleanup_runtime_state,
    )

    await _handler(
        db,
        session_manager=session_manager,
        session_coordinator=session_coordinator,
    ).post_terminal_cleanup(
        _run(task_id=None, terminal_reason="daemon_stop"),
        allow_parent_session_fallback=False,
    )

    session_coordinator.release_session_worktrees.assert_not_called()
    session_manager.update_status.assert_not_called()
    assert runtime_calls == [(db, "run-1", "child-1", "daemon_stop")]
    assert db.executed == []


async def test_daemon_stop_parking_with_task_retains_isolation_and_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    released_sessions: list[str] = []
    released_clones: list[str] = []
    artifact_tasks: list[str] = []

    class SessionCoordinator:
        def release_session_worktrees(self, session_id: str) -> None:
            released_sessions.append(session_id)

    class CloneStorage:
        def release(self, clone_id: str) -> None:
            released_clones.append(clone_id)

    def cleanup_artifacts(
        _db: object,
        task_id: str,
        *,
        preserve_worktree_id: str | None = None,
    ) -> list[object]:
        artifact_tasks.append(task_id)
        return []

    monkeypatch.setattr(
        terminal_cleanup,
        "cleanup_merged_task_artifacts_after_agent_exit",
        cleanup_artifacts,
    )
    _stub_runtime_cleanup(monkeypatch)
    handler = _handler(
        db,
        session_coordinator=SessionCoordinator(),
        clone_storage=CloneStorage(),
    )

    await handler.post_terminal_cleanup(
        replace(_run(status="cancelled", terminal_reason="daemon_stop"), clone_id="clone-1"),
        allow_parent_session_fallback=False,
    )

    assert released_sessions == []
    assert released_clones == []
    assert artifact_tasks == []
    assert db.executed == []


async def test_daemon_stop_parking_with_task_preserves_completion_subscribers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    registry = AcknowledgingCompletionRegistry({"child-1": True})
    _stub_runtime_cleanup(monkeypatch)
    handler = _handler(db, completion_registry=registry)

    await handler.post_terminal_cleanup(
        _run(status="cancelled", terminal_reason="daemon_stop"),
        allow_parent_session_fallback=False,
        notification_result={"status": "cancelled", "terminal_reason": "daemon_stop"},
        notification_message="Agent run-1 cancelled",
    )

    assert registry.notifications == []
    assert registry.cleaned == []
    assert db.executed == []


async def test_subscriber_notify_failure_does_not_abort_terminal_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    registry = _FailingNotifyRegistry()
    session_coordinator = MagicMock()
    artifact_calls: list[tuple[object, str]] = []

    def retry_cleanup(
        cleanup_db: object,
        task_id: str,
        *,
        preserve_worktree_id: str | None = None,
    ) -> list[SimpleNamespace]:
        artifact_calls.append((cleanup_db, task_id))
        return []

    monkeypatch.setattr(
        terminal_cleanup,
        "cleanup_merged_task_artifacts_after_agent_exit",
        retry_cleanup,
    )
    _stub_runtime_cleanup(monkeypatch)
    handler = _handler(db, completion_registry=registry, session_coordinator=session_coordinator)

    await handler.post_terminal_cleanup(
        _run(),
        allow_parent_session_fallback=False,
        notification_result={"status": "completed"},
        notification_message="done",
    )

    assert registry.cleaned == []
    session_coordinator.release_session_worktrees.assert_called_once_with("child-1")
    assert artifact_calls == [(db, "task-1")]
    assert db.executed == []


async def test_post_terminal_cleanup_missing_child_does_not_target_parent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    session_manager = MagicMock()
    session_coordinator = MagicMock()
    monkeypatch.setattr(
        "gobby.agents.runtime_cleanup.cleanup_agent_runtime_state",
        lambda *args, **kwargs: SimpleNamespace(dispatch_mutex_rows=0, workflow_instance_rows=0),
    )

    await _handler(
        db,
        session_manager=session_manager,
        session_coordinator=session_coordinator,
    ).post_terminal_cleanup(
        _run(task_id=None, child_session_id=None),
        allow_parent_session_fallback=False,
    )

    session_coordinator.release_session_worktrees.assert_not_called()
    session_manager.update_status.assert_not_called()
    assert db.executed == []
