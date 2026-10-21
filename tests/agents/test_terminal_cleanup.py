from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import terminal_cleanup
from gobby.agents.sandbox_reaper import SandboxReapResult
from gobby.storage.hub import operation_deadline
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
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


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["runtime_probe", "terminal_settlement"])
async def test_nested_terminal_close_does_not_log_exception_text(
    phase: str, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "private marker"
    run = replace(_run(task_id=None), terminal_id="terminal-1")
    agent_run_manager = MagicMock()
    agent_run_manager.get.return_value = run
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(id="terminal-1", state="live")
    if phase == "terminal_settlement":
        manager.mark_exited.side_effect = RuntimeError(marker)
    terminal_services = MagicMock()
    handler = _handler(
        RecordingDb(), agent_run_manager=agent_run_manager, terminal_services=terminal_services
    )
    probe = AsyncMock(
        side_effect=RuntimeError(marker) if phase == "runtime_probe" else None,
        return_value=False,
    )

    with (
        caplog.at_level("WARNING", logger="gobby.agents.terminal_cleanup"),
        patch("gobby.storage.terminals.TerminalManager", return_value=manager),
        patch("gobby.agents.capture.backend_session_present", new=probe),
    ):
        closed = await handler._resource_cleaner._close_tmux_session(run)

    assert closed is False
    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_terminal_cleanup_failure_logs_do_not_include_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "private marker"
    _stub_runtime_cleanup(monkeypatch)
    session_coordinator = MagicMock()
    session_coordinator.release_session_worktrees.side_effect = RuntimeError(marker)
    handler = _handler(
        RecordingDb(),
        completion_registry=MagicMock(),
        session_coordinator=session_coordinator,
    )

    with (
        caplog.at_level("WARNING", logger="gobby.agents.terminal_cleanup"),
        patch(
            "gobby.agents.terminal_delivery.deliver_and_cleanup_terminal_run",
            new=AsyncMock(side_effect=RuntimeError(marker)),
        ),
    ):
        await handler.post_terminal_cleanup(
            _run(task_id=None),
            notification_result={"status": "error"},
            notification_message="Agent failed",
        )

    assert "Failed to deliver terminal result" in caplog.text
    assert "Failed to release worktrees" in caplog.text
    assert "RuntimeError" in caplog.text
    assert marker not in caplog.text


@pytest.fixture(autouse=True)
def _stub_srt_runner_reap(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reap(_run_id: str) -> int:
        return 0

    async def reap_roots(_run_id: str) -> SandboxReapResult:
        return SandboxReapResult()

    monkeypatch.setattr(terminal_cleanup, "reap_srt_runner_process_tree", reap)
    monkeypatch.setattr(terminal_cleanup, "reap_sandbox_run_roots", reap_roots)


async def test_terminal_worktree_release_runs_off_event_loop() -> None:
    loop_thread = threading.get_ident()
    release_threads: list[int] = []
    coordinator = MagicMock()
    coordinator.release_session_worktrees.side_effect = lambda _session_id: release_threads.append(
        threading.get_ident()
    )
    await _handler(
        RecordingDb(), run_db=asyncio.to_thread, session_coordinator=coordinator
    ).post_terminal_cleanup(_run(task_id=None))
    assert len(release_threads) == 1
    assert release_threads[0] != loop_thread


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

    class SessionCoordinator:
        def release_session_worktrees(self, session_id: str) -> None:
            released_sessions.append(session_id)

    class CloneStorage:
        def release(self, clone_id: str) -> None:
            released_clones.append(clone_id)

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
    assert db.executed == []


@pytest.mark.integration
async def test_exhausted_inherited_deadline_still_releases_the_dispatch_mutex(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#22016: a spent deadline must not strand the same-task spawn mutex."""
    task_manager = LocalTaskManager(temp_db)
    task = task_manager.create_task(
        sample_project["id"],
        "Terminal run holding a spawn mutex",
        validation_criteria="The dispatch mutex is released after terminal cleanup.",
    )
    run_id = "dddddddd-dddd-4ddd-8ddd-dddddddd2197"
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder=f"spawn-agent:{run_id}",
        kind="spawn_agent",
        ttl_seconds=600,
        run_id=run_id,
    )
    handler = _handler(
        temp_db,
        completion_registry=_FailingNotifyRegistry(),
        session_coordinator=MagicMock(),
    )
    run = replace(_run(task_id=task.id), id=run_id)

    now = time.monotonic()
    monkeypatch.setattr(operation_deadline, "time", SimpleNamespace(monotonic=lambda: now))

    with database_operation_deadline(timeout_seconds=0.04):
        now += 0.06
        await handler.post_terminal_cleanup(
            run,
            allow_parent_session_fallback=False,
            notification_result={"status": "completed"},
            notification_message="done",
        )

    assert mutexes.get_mutex(task.id) is None
    assert mutexes.get_mutex_by_run_id(run_id) is None
    # The replacement spawn takes the mutex without needing kill_agent first.
    replacement_run_id = "dddddddd-dddd-4ddd-8ddd-dddddddd2198"
    assert mutexes.acquire_mutex(
        task.id,
        holder=f"spawn-agent:{replacement_run_id}",
        kind="spawn_agent",
        ttl_seconds=600,
        run_id=replacement_run_id,
    )


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


@pytest.mark.asyncio
async def test_post_terminal_cleanup_records_retained_sandbox_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[object, str, dict[str, str]]] = []

    async def reap_roots(_run_id: str) -> SandboxReapResult:
        return SandboxReapResult(
            retained_violation_log=Path("/gobby-home/logs/sandbox-violations/run-1.jsonl"),
            retained_settings=Path("/gobby-home/logs/sandbox-violations/run-1.settings.json"),
        )

    monkeypatch.setattr(terminal_cleanup, "reap_sandbox_run_roots", reap_roots)
    monkeypatch.setattr(
        terminal_cleanup,
        "record_sandbox_retention",
        lambda db, run_id, retention: recorded.append((db, run_id, retention)),
    )
    agent_run_manager = MagicMock()
    handler = _handler(RecordingDb(), agent_run_manager=agent_run_manager)

    await handler.post_terminal_cleanup(_run(task_id=None), allow_parent_session_fallback=False)

    assert recorded == [
        (
            agent_run_manager.db,
            "run-1",
            {
                "retained_violation_path": "/gobby-home/logs/sandbox-violations/run-1.jsonl",
                "retained_settings_path": "/gobby-home/logs/sandbox-violations/run-1.settings.json",
            },
        )
    ]
