from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import terminal_cleanup, terminal_delivery
from gobby.agents.agent_cleanup import AgentCleanupHandler
from gobby.storage.agents import AgentRun, AgentRunStatus, AgentRunTerminalReason
from gobby.storage.terminals import Terminal
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.runtime import SnapshotResult
from gobby.terminals.services import TerminalServices
from tests.agents.cleanup_test_support import (
    AcknowledgingCompletionRegistry,
    RecordingDb,
    _handler,
    _RecordingTaskRecovery,
    _run,
    _stub_runtime_cleanup,
)
from tests.agents.test_capture import FakeCaptureStorage
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore, make_memory_terminal

pytestmark = pytest.mark.unit


async def test_acknowledged_stale_sweeps_deliver_each_transitioned_run() -> None:
    run_manager = MagicMock()
    run_manager.cleanup_stale_runs.return_value = ["run-timeout"]
    run_manager.cleanup_stale_pending_runs.return_value = ["run-pending"]
    run_manager.get.side_effect = [
        SimpleNamespace(id="run-timeout", status="timeout", error="stale running"),
        SimpleNamespace(id="run-pending", status="error", error="stale pending"),
    ]
    registry = MagicMock()
    handler = _handler(
        MagicMock(),
        agent_run_manager=run_manager,
        completion_registry=registry,
    )

    with patch.object(
        terminal_delivery,
        "deliver_and_cleanup_terminal_run",
        new_callable=AsyncMock,
    ) as deliver:
        run_ids = await handler.run_acknowledged_stale_sweeps(
            machine_id="machine-local",
            running_timeout_minutes=30,
            pending_timeout_minutes=60,
        )

    assert run_ids == ["run-timeout", "run-pending"]
    run_manager.cleanup_stale_runs.assert_called_once_with(
        machine_id="machine-local",
        default_timeout_minutes=30,
    )
    run_manager.cleanup_stale_pending_runs.assert_called_once_with(
        machine_id="machine-local",
        timeout_minutes=60,
        long_timeout_minutes=1440,
    )
    assert [call.kwargs["result"] for call in deliver.await_args_list] == [
        {"status": "timeout", "run_id": "run-timeout", "error": "stale running"},
        {"status": "error", "run_id": "run-pending", "error": "stale pending"},
    ]


async def test_daemon_stop_terminalization_with_task_keeps_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    active = _run(status="running")
    parked = _run(status="cancelled", terminal_reason="daemon_stop")
    manager = MagicMock()
    manager.get.return_value = active
    manager.cancel.return_value = parked
    task_recovery = _RecordingTaskRecovery()
    tick_calls: list[tuple[str, str]] = []

    def record_tick(_db: object, *, task_id: str, reason: str) -> None:
        tick_calls.append((task_id, reason))

    _stub_runtime_cleanup(monkeypatch)
    monkeypatch.setattr(
        "gobby.build.dispatch_tick.schedule_dispatcher_tick_for_task",
        record_tick,
    )
    handler = _handler(db, agent_run_manager=manager, task_recovery=task_recovery)

    assert await handler.terminalize_cancelled_run("run-1", terminal_reason="daemon_stop")

    manager.cancel.assert_called_once_with("run-1", terminal_reason="daemon_stop")
    assert task_recovery.recovered == []
    assert tick_calls == [("task-1", "agent_parked")]


async def test_user_cancelled_terminalization_with_task_runs_full_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = RecordingDb()
    active = replace(_run(status="running"), clone_id="clone-1")
    cancelled = replace(
        _run(status="cancelled", terminal_reason="user_cancelled"),
        clone_id="clone-1",
    )
    manager = MagicMock()
    manager.get.return_value = active
    manager.cancel.return_value = cancelled
    task_recovery = _RecordingTaskRecovery()
    registry = AcknowledgingCompletionRegistry({"child-1": True})
    session_coordinator = MagicMock()
    clone_storage = MagicMock()
    artifact_calls: list[tuple[object, str]] = []
    tick_calls: list[tuple[str, str]] = []

    def retry_cleanup(
        cleanup_db: object,
        task_id: str,
        *,
        preserve_worktree_id: str | None = None,
    ) -> list[SimpleNamespace]:
        artifact_calls.append((cleanup_db, task_id))
        return []

    def record_tick(_db: object, *, task_id: str, reason: str) -> None:
        tick_calls.append((task_id, reason))

    monkeypatch.setattr(
        terminal_cleanup,
        "cleanup_merged_task_artifacts_after_agent_exit",
        retry_cleanup,
    )
    _stub_runtime_cleanup(monkeypatch)
    monkeypatch.setattr(
        "gobby.build.dispatch_tick.schedule_dispatcher_tick_for_task",
        record_tick,
    )
    handler = _handler(
        db,
        agent_run_manager=manager,
        completion_registry=registry,
        session_coordinator=session_coordinator,
        task_recovery=task_recovery,
        clone_storage=clone_storage,
    )

    assert await handler.terminalize_cancelled_run("run-1", terminal_reason="user_cancelled")

    assert task_recovery.recovered == [(cancelled, "cancelled")]
    session_coordinator.release_session_worktrees.assert_called_once_with("child-1")
    clone_storage.release.assert_called_once_with("clone-1")
    assert artifact_calls == [(db, "task-1")]
    assert registry.notifications == [
        (
            "run-1",
            {"status": "cancelled", "terminal_reason": "user_cancelled", "run_id": "run-1"},
            "Agent run-1 cancelled",
        )
    ]
    assert registry.cleaned == ["run-1"]
    assert tick_calls == [("task-1", "agent_cancelled")]
    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["child-1"]),
        )
    ]


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
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> AgentRun | None:
        return self._terminal(run_id, "success")


class _StickyRuntime(FakeRuntime):
    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        del grace_seconds
        name = terminal.session_name or terminal.spawn_key
        if name is not None:
            self.killed.append(name)


class _RemainOnExitRuntime(FakeRuntime):
    """Pane process has exited; remain-on-exit kept the tmux session."""

    async def is_live(self, terminal: Terminal) -> bool:
        del terminal
        return False

    async def session_present(self, terminal: Terminal) -> bool:
        return terminal.id not in self.killed_ids


def _capture_services(
    *,
    live: bool = True,
    kill_succeeds: bool = True,
    pane_dead: bool = False,
) -> tuple[TerminalServices, FakeRuntime]:
    terminal = make_memory_terminal(terminal_id="wf-live", session_name="wf-live")
    if not live:
        terminal.state = "exited"
    store = MemoryTerminalStore(terminal)
    runtime: FakeRuntime
    if pane_dead:
        runtime = _RemainOnExitRuntime()
    elif kill_succeeds:
        runtime = FakeRuntime()
    else:
        runtime = _StickyRuntime()
    runtime.snapshot_text = "pane output"
    runtime.snapshot_full_result = SnapshotResult(
        text="pane output",
        truncated=False,
        dropped_bytes=0,
        total_bytes=len(b"pane output"),
    )
    if live and not pane_dead:
        runtime.live_keys.add("wf-live")
    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    return TerminalServices(manager=store, registry=registry), runtime


def _tmux_run(status: AgentRunStatus = "running") -> AgentRun:
    return replace(
        _run(task_id=None, child_session_id=None, status=status),
        terminal_id="wf-live",
    )


def _wrapper_handler(
    storage: _CaptureWrapperStorage,
    services: TerminalServices,
) -> tuple[AgentCleanupHandler, list[AgentRun]]:
    handler = _handler(MagicMock(), agent_run_manager=storage, terminal_services=services)
    cleanup_runs: list[AgentRun] = []

    async def post_terminal_cleanup(run: AgentRun, **kwargs: object) -> None:
        cleanup_runs.append(run)

    handler.post_terminal_cleanup = post_terminal_cleanup  # type: ignore[method-assign]
    return handler, cleanup_runs


async def test_terminalize_successful_run_captures_live_tmux_before_kill() -> None:
    storage = _CaptureWrapperStorage(_tmux_run())
    services, runtime = _capture_services()
    handler, cleanup_runs = _wrapper_handler(storage, services)

    assert await handler.terminalize_successful_run(
        "run-1",
        notify_result={"status": "completed"},
        message="done",
    )

    assert runtime.killed == ["wf-live"]
    stored = storage.get("run-1")
    assert stored is not None
    assert stored.status == "success"
    assert "pane output" in (stored.result or "")
    assert storage.events.index("persist:run-1") < storage.events.index("terminal:run-1:success")
    assert cleanup_runs and cleanup_runs[0].status == "success"


async def test_terminalize_successful_run_kill_failure_keeps_run_nonterminal() -> None:
    storage = _CaptureWrapperStorage(_tmux_run())
    services, _runtime = _capture_services(kill_succeeds=False)
    handler, cleanup_runs = _wrapper_handler(storage, services)

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
    services, runtime = _capture_services()
    handler, cleanup_runs = _wrapper_handler(storage, services)

    assert await handler.terminalize_cancelled_run(
        "run-1",
        terminal_reason="user_cancelled",
    )

    assert runtime.killed == ["wf-live"]
    stored = storage.get("run-1")
    assert stored is not None
    assert stored.status == "cancelled"
    assert "pane output" in (stored.result or "")
    assert cleanup_runs and cleanup_runs[0].status == "cancelled"


async def test_terminalize_successful_run_kills_remain_on_exit_session() -> None:
    storage = _CaptureWrapperStorage(_tmux_run())
    services, runtime = _capture_services(pane_dead=True)
    handler, cleanup_runs = _wrapper_handler(storage, services)

    assert await handler.terminalize_successful_run(
        "run-1",
        notify_result={"status": "completed"},
        message="done",
    )

    assert runtime.killed == ["wf-live"]
    stored = storage.get("run-1")
    assert stored is not None
    assert stored.status == "success"
    assert "pane output" in (stored.result or "")
    assert cleanup_runs and cleanup_runs[0].status == "success"
    assert await runtime.session_present(services.manager.rows["wf-live"]) is False


async def test_terminalize_wrappers_skip_policy_when_session_absent() -> None:
    storage = _CaptureWrapperStorage(_tmux_run())
    services, runtime = _capture_services(live=False)
    handler, cleanup_runs = _wrapper_handler(storage, services)

    assert await handler.terminalize_successful_run(
        "run-1",
        notify_result={"status": "completed"},
        message="done",
    )

    assert runtime.killed == []
    assert not any(event.startswith("intent:") for event in storage.events)
    assert not any(event.startswith("persist:") for event in storage.events)
    stored = storage.get("run-1")
    assert stored is not None
    assert stored.status == "success"
    assert cleanup_runs and cleanup_runs[0].status == "success"
