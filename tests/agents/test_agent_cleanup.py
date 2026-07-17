from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import agent_cleanup
from gobby.agents.agent_cleanup import (
    AgentCleanupHandler,
    cleanup_merged_task_artifacts_after_agent_exit,
)
from gobby.storage.agents import AgentRun
from tests.agents.test_capture import FakeCaptureStorage

pytestmark = pytest.mark.unit


class RecordingDb:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> SimpleNamespace:
        self.executed.append((sql, params))
        return SimpleNamespace(rowcount=1)


class RecordingCompletionRegistry:
    def __init__(self) -> None:
        self.cleaned: list[str] = []

    def cleanup(self, completion_id: str) -> None:
        self.cleaned.append(completion_id)


def _run(
    task_id: str | None = "task-1",
    *,
    child_session_id: str | None = "child-1",
    status: str = "success",
    tool_calls_count: int = 0,
    turns_used: int = 0,
    reused_worktree: bool = False,
) -> AgentRun:
    return AgentRun(
        id="run-1",
        parent_session_id="parent-1",
        child_session_id=child_session_id,
        provider="codex",
        prompt="test",
        status=status,
        created_at="2026-05-20T00:00:00+00:00",
        updated_at="2026-05-20T00:00:00+00:00",
        task_id=task_id,
        worktree_id="wt-1",
        tool_calls_count=tool_calls_count,
        turns_used=turns_used,
        resume_metadata_json={"initial_variables": {"reused_worktree": True}}
        if reused_worktree
        else None,
    )


def _handler(
    db: object,
    run_db=None,
    *,
    agent_run_manager=None,
    completion_registry=None,
    session_manager=None,
    session_coordinator=None,
    task_recovery=None,
) -> AgentCleanupHandler:
    async def default_run_db(func, *args, **kwargs):
        return func(*args, **kwargs)

    clearable = MagicMock()
    return AgentCleanupHandler(
        agent_run_manager=agent_run_manager or MagicMock(),
        db=db,
        get_session_manager=lambda: session_manager,
        get_session_coordinator=lambda: session_coordinator,
        clone_storage=None,
        completion_registry=completion_registry,
        task_recovery=task_recovery or AsyncMock(),
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


@pytest.mark.asyncio
async def test_post_terminal_cleanup_retries_merge_artifact_cleanup_for_task_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
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

    await _handler(db).post_terminal_cleanup(_run(), allow_parent_session_fallback=False)

    assert calls == [(db, "task-1")]
    assert db.executed == [
        ("DELETE FROM completion_subscribers WHERE completion_id = %s", ("run-1",))
    ]


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
        agent_cleanup,
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


@pytest.mark.asyncio
async def test_post_terminal_cleanup_skips_merge_artifact_cleanup_without_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
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

    result = await _handler(db).post_terminal_cleanup(
        _run(task_id=None), allow_parent_session_fallback=False
    )

    assert result is None
    cleanup.assert_not_called()
    assert db.executed == [
        ("DELETE FROM completion_subscribers WHERE completion_id = %s", ("run-1",))
    ]


@pytest.mark.asyncio
async def test_post_terminal_cleanup_clears_completion_registry_and_subscribers(
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

    assert registry.cleaned == ["run-1"]
    assert db.executed == [
        ("DELETE FROM completion_subscribers WHERE completion_id = %s", ("run-1",))
    ]


@pytest.mark.asyncio
async def test_post_terminal_cleanup_subscriber_failure_does_not_stop_later_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    session_manager = MagicMock()
    session_coordinator = MagicMock()
    runtime_calls: list[tuple[object, str, str | None]] = []

    def fail_subscriber_cleanup(**_kwargs: object) -> None:
        raise RuntimeError("subscriber cleanup failed")

    def cleanup_runtime_state(
        cleanup_db: object,
        *,
        run_id: str,
        child_session_id: str | None,
    ) -> SimpleNamespace:
        runtime_calls.append((cleanup_db, run_id, child_session_id))
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
    ).post_terminal_cleanup(_run(task_id=None), allow_parent_session_fallback=False)

    session_coordinator.release_session_worktrees.assert_called_once_with("child-1")
    session_manager.update_status.assert_called_once_with("child-1", "expired")
    assert runtime_calls == [(db, "run-1", "child-1")]
    assert db.executed == []


@pytest.mark.asyncio
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
    assert db.executed == [
        ("DELETE FROM completion_subscribers WHERE completion_id = %s", ("run-1",))
    ]


@pytest.mark.asyncio
async def test_cleanup_agent_failure_persists_child_session_progress_stats() -> None:
    failed_run = _run(status="error", tool_calls_count=7, turns_used=4)
    recovered: list[tuple[AgentRun, str]] = []
    cleanup_runs: list[AgentRun] = []

    class RunManager:
        failed_with: dict[str, object] | None = None

        def fail(self, run_id: str, **kwargs: object) -> AgentRun:
            self.failed_with = {"run_id": run_id, **kwargs}
            return failed_run

    class SessionManager:
        def get(self, session_id: str) -> SimpleNamespace:
            assert session_id == "child-1"
            return SimpleNamespace(tool_call_count=7, turn_count=4)

    class TaskRecovery:
        async def recover_task_from_terminal_agent(self, run: AgentRun, *, outcome: str) -> None:
            recovered.append((run, outcome))

    run_manager = RunManager()
    handler = _handler(
        object(),
        agent_run_manager=run_manager,
        session_manager=SessionManager(),
        task_recovery=TaskRecovery(),
    )

    async def post_terminal_cleanup(run: AgentRun, **kwargs: object) -> None:
        cleanup_runs.append(run)

    handler.post_terminal_cleanup = post_terminal_cleanup  # type: ignore[method-assign]
    terminal_payload = "Agent idle: idle after max reprompt attempts"

    await handler.cleanup_agent(_run(status="running"), terminal_payload=terminal_payload)

    assert run_manager.failed_with == {
        "run_id": "run-1",
        "error": terminal_payload,
        "tool_calls_count": 7,
        "turns_used": 4,
    }
    assert recovered == [(failed_run, "failed")]
    assert cleanup_runs == [failed_run]


class _CaptureWrapperStorage(FakeCaptureStorage):
    """Capture-policy storage whose complete() matches the manager signature."""

    def complete(
        self,
        run_id: str,
        result: str | None = None,
        tool_calls_count: int = 0,
        turns_used: int = 0,
    ) -> AgentRun | None:
        return self._terminal(run_id, "success")


class _FakeTmux:
    def __init__(self, *, kill_succeeds: bool = True, alive: bool = True) -> None:
        self.alive = alive
        self.kill_succeeds = kill_succeeds
        self.kills: list[str] = []

    async def has_session(self, name: str) -> bool:
        return self.alive

    async def capture_full_pane(self, session_name: str) -> str | None:
        return "pane output" if self.alive else None

    async def kill_session(
        self,
        name: str,
        *,
        missing_ok: bool = False,
        timeout: float = 5.0,
    ) -> bool:
        self.kills.append(name)
        if self.kill_succeeds:
            self.alive = False
        return self.kill_succeeds


def _tmux_run(status: str = "running") -> AgentRun:
    return replace(
        _run(task_id=None, child_session_id=None, status=status),
        tmux_session_name="wf-live",
    )


def _wrapper_handler(storage: _CaptureWrapperStorage) -> tuple[AgentCleanupHandler, list[AgentRun]]:
    handler = _handler(MagicMock(), agent_run_manager=storage)
    cleanup_runs: list[AgentRun] = []

    async def post_terminal_cleanup(run: AgentRun, **kwargs: object) -> None:
        cleanup_runs.append(run)

    handler.post_terminal_cleanup = post_terminal_cleanup  # type: ignore[method-assign]
    return handler, cleanup_runs


async def test_terminalize_successful_run_captures_live_tmux_before_kill() -> None:
    storage = _CaptureWrapperStorage(_tmux_run())
    tmux = _FakeTmux()
    handler, cleanup_runs = _wrapper_handler(storage)

    with patch("gobby.agents.tmux.get_tmux_session_manager", return_value=tmux):
        assert await handler.terminalize_successful_run(
            "run-1",
            notify_result={"status": "completed"},
            message="done",
        )

    assert tmux.kills == ["wf-live"]
    stored = storage.get("run-1")
    assert stored is not None
    assert stored.status == "success"
    assert "pane output" in (stored.result or "")
    assert storage.events.index("persist:run-1") < storage.events.index("terminal:run-1:success")
    assert cleanup_runs and cleanup_runs[0].status == "success"


async def test_terminalize_successful_run_kill_failure_keeps_run_nonterminal() -> None:
    storage = _CaptureWrapperStorage(_tmux_run())
    tmux = _FakeTmux(kill_succeeds=False)
    handler, cleanup_runs = _wrapper_handler(storage)

    with patch("gobby.agents.tmux.get_tmux_session_manager", return_value=tmux):
        assert not await handler.terminalize_successful_run(
            "run-1",
            notify_result={"status": "completed"},
            message="done",
        )

    stored = storage.get("run-1")
    assert stored is not None
    assert stored.status == "running"
    assert stored.pending_terminal_action == "complete"
    assert "pane output" in (stored.result or "")
    assert cleanup_runs == []


async def test_terminalize_cancelled_run_captures_live_tmux_before_kill() -> None:
    storage = _CaptureWrapperStorage(_tmux_run())
    tmux = _FakeTmux()
    handler, cleanup_runs = _wrapper_handler(storage)

    with patch("gobby.agents.tmux.get_tmux_session_manager", return_value=tmux):
        assert await handler.terminalize_cancelled_run(
            "run-1",
            terminal_reason="user_cancelled",
        )

    assert tmux.kills == ["wf-live"]
    stored = storage.get("run-1")
    assert stored is not None
    assert stored.status == "cancelled"
    assert "pane output" in (stored.result or "")
    assert cleanup_runs and cleanup_runs[0].status == "cancelled"


async def test_terminalize_wrappers_skip_policy_when_session_absent() -> None:
    storage = _CaptureWrapperStorage(_tmux_run())
    tmux = _FakeTmux(alive=False)
    handler, cleanup_runs = _wrapper_handler(storage)

    with patch("gobby.agents.tmux.get_tmux_session_manager", return_value=tmux):
        assert await handler.terminalize_successful_run(
            "run-1",
            notify_result={"status": "completed"},
            message="done",
        )

    assert tmux.kills == []
    assert not any(event.startswith("intent:") for event in storage.events)
    assert not any(event.startswith("persist:") for event in storage.events)
    stored = storage.get("run-1")
    assert stored is not None
    assert stored.status == "success"
    assert cleanup_runs and cleanup_runs[0].status == "success"
