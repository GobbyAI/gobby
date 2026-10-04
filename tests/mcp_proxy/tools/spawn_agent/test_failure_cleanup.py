"""Spawn failure cleanup: lost-CAS tolerance, kill truth, one attempt, cancellation."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import spawn_executor
from gobby.agents.isolation import SpawnConfig, WorktreeIsolationHandler
from gobby.mcp_proxy.tools.spawn_agent import _failure_cleanup
from gobby.mcp_proxy.tools.spawn_agent._spawn_phase import SpawnPhase
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.worktrees.git import WorktreeGitManager
from tests.terminals.fakes import (
    FakeRuntime,
    MemoryTerminalStore,
    make_memory_terminal,
    runtime_registry,
)

pytestmark = pytest.mark.unit


def _worktree_spawn_config(branch_name: str) -> SpawnConfig:
    return SpawnConfig(
        prompt="Test",
        task_id="task-123",
        task_title="Atomic spawn",
        task_seq_num=123,
        branch_name=branch_name,
        branch_prefix=None,
        base_branch="main",
        project_id="project-123",
        project_path="/repo",
        provider="claude",
        parent_session_id="session-123",
    )


@pytest.mark.asyncio
async def test_fresh_worktree_deleted_on_post_prepare_failure() -> None:
    git_manager = MagicMock(spec=WorktreeGitManager, repo_path="/repo")
    git_manager.get_current_branch.return_value = "main"
    git_manager.has_unpushed_commits.return_value = (False, 0)
    git_manager.create_worktree.return_value = SimpleNamespace(success=True)
    worktree_storage = MagicMock(db=MagicMock())
    worktree_storage.get_by_branch.return_value = None
    worktree_storage.create.return_value = SimpleNamespace(
        id="fresh-wt",
        worktree_path="/tmp/fresh-wt",
        branch_name="feature/fresh",
    )
    handler = WorktreeIsolationHandler(git_manager, worktree_storage)
    config = _worktree_spawn_config("feature/fresh")
    artifact_manager = MagicMock()

    with (
        patch.object(handler, "_generate_worktree_path", return_value="/tmp/fresh-wt"),
        patch(
            "gobby.agents.isolation_worktree.repair_isolation_environment",
            new=AsyncMock(),
        ) as repair_isolation,
        patch(
            "gobby.agents.isolation_worktree.worktree_reuse.capture_worktree_base_commit_sha",
            return_value="base-sha",
        ) as capture_base_commit,
        patch(
            "gobby.agents.isolation_worktree.TaskArtifactManager",
            return_value=artifact_manager,
        ),
    ):
        context = await handler.prepare_environment(config)
        await handler.cleanup_environment(config)

    assert context.worktree_id == "fresh-wt"
    assert context.cwd == "/tmp/fresh-wt"
    assert context.branch_name == "feature/fresh"
    assert context.extra == {"base_commit_sha": "base-sha", "main_repo_path": "/repo"}
    capture_base_commit.assert_called_once_with(
        git_manager=git_manager,
        worktree_path="/tmp/fresh-wt",
        base_branch="main",
        use_local=False,
    )
    repair_isolation.assert_awaited_once_with(
        main_repo_path="/repo",
        isolated_path="/tmp/fresh-wt",
        provider="claude",
    )
    git_manager.delete_worktree.assert_called_once_with(
        worktree_path="/tmp/fresh-wt",
        force=True,
        delete_branch=True,
        force_delete_branch=True,
        branch_name="feature/fresh",
    )
    artifact_manager.clear_worktree_references.assert_called_once_with("fresh-wt")
    worktree_storage.delete.assert_called_once_with("fresh-wt")


@pytest.mark.asyncio
async def test_reused_worktree_survives_failure() -> None:
    git_manager = MagicMock(spec=WorktreeGitManager, repo_path="/repo")
    git_manager.get_current_branch.return_value = "main"
    worktree_storage = MagicMock(db=MagicMock())
    worktree_storage.get_by_branch.return_value = SimpleNamespace(
        id="reused-wt",
        worktree_path="/tmp/reused-wt",
        branch_name="feature/reused",
    )
    worktree_storage.is_claimed_by_live_session.return_value = False
    handler = WorktreeIsolationHandler(git_manager, worktree_storage)
    config = _worktree_spawn_config("feature/reused")

    with (
        patch("gobby.agents.isolation_worktree.Path.is_dir", return_value=True),
        patch(
            "gobby.agents.isolation_worktree.worktree_reuse.sync_reused_worktree_to_base",
            new=AsyncMock(return_value=SimpleNamespace(base_commit_sha="base-sha")),
        ) as sync_reused_worktree,
        patch(
            "gobby.agents.isolation_worktree.repair_isolation_environment",
            new=AsyncMock(),
        ) as repair_isolation,
    ):
        context = await handler.prepare_environment(config)
        await handler.cleanup_environment(config)

    assert context.worktree_id == "reused-wt"
    assert context.cwd == "/tmp/reused-wt"
    assert context.branch_name == "feature/reused"
    assert context.extra == {"base_commit_sha": "base-sha", "main_repo_path": "/repo"}
    sync_reused_worktree.assert_awaited_once()
    repair_isolation.assert_awaited_once_with(
        main_repo_path="/repo",
        isolated_path="/tmp/reused-wt",
        provider="claude",
    )
    git_manager.create_worktree.assert_not_called()
    git_manager.delete_worktree.assert_not_called()
    worktree_storage.delete.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_failure_envelope_includes_isolation_identity() -> None:
    from gobby.mcp_proxy.tools.spawn_agent._execution import finalize_executed_spawn

    run_storage = MagicMock()
    runner = SimpleNamespace(run_storage=run_storage)
    spawn_result = SimpleNamespace(
        success=False,
        error="provider boot failed",
        child_session_id="child-123",
        backend="none",
        pid=None,
        retryable_infrastructure=False,
        prior_attempt=None,
    )
    isolation_context = SimpleNamespace(
        worktree_id="worktree-123",
        clone_id=None,
        branch_name="feature/atomic-spawn",
    )
    handler = object()
    spawn_config = object()
    reasoning = SimpleNamespace(to_dict=lambda: {"status": "not_requested"})

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._execution.cleanup_failed_spawn",
        new_callable=AsyncMock,
    ) as cleanup:
        result = await finalize_executed_spawn(
            runner=runner,
            run_id="run-123",
            spawn_result=spawn_result,
            spawn_request=None,
            isolation_ctx=isolation_context,
            effective_isolation="worktree",
            base_commit_sha="base-sha",
            handler=handler,
            spawn_config=spawn_config,
            completion_registry=None,
            cleanup_isolation_on_failure=True,
            task_manager=None,
            session_manager=None,
            parent_session_id="parent-123",
            effective_provider="claude",
            resolved_task_id=None,
            task_seq_num=None,
            db=None,
            agent_body=None,
            effective_initial_variables={},
            reasoning=reasoning,
        )

    assert result == {
        "success": False,
        "error": "provider boot failed",
        "run_id": "run-123",
        "worktree_id": "worktree-123",
        "branch_name": "feature/atomic-spawn",
        "reasoning": {"status": "not_requested"},
    }
    run_storage.update_child_session.assert_called_once_with("run-123", "child-123")
    run_storage.update_runtime.assert_called_once_with(
        "run-123",
        pid=None,
        terminal_id=None,
        worktree_id="worktree-123",
        clone_id=None,
    )
    cleanup.assert_awaited_once_with(
        runner,
        "run-123",
        "provider boot failed",
        handler,
        spawn_config,
        completion_registry=None,
        cleanup_isolation=True,
        task_manager=None,
        child_session_id="child-123",
        pid=None,
        terminal_id=None,
        prior_attempt=None,
        cleanup_once=None,
        attempt_terminal_known=True,
    )


@pytest.mark.asyncio
async def test_spawn_rollback_uses_shared_cancelled_terminalization() -> None:
    events: list[str] = []
    terminalize_arguments: dict[str, Any] = {}

    class RunStorage:
        db = object()

        def record_spawn_error(self, run_id: str, error: str) -> None:
            assert (run_id, error) == ("run-1", "spawn failed")
            events.append("record-error")

        def get(self, _run_id: str) -> None:
            return None

    async def terminalize(**kwargs: Any) -> bool:
        events.append("terminalize")
        terminalize_arguments.update(kwargs)
        return True

    class Handler:
        async def cleanup_environment(self, _spawn_config: object) -> None:
            events.append("cleanup-isolation")

    def delete_child(*_args: Any, **_kwargs: Any) -> None:
        events.append("delete-child")

    run_storage = RunStorage()
    runner = SimpleNamespace(run_storage=run_storage, agent_lifecycle_monitor=None)
    handler = Handler()
    completion_registry = object()
    task_manager = object()
    with (
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            terminalize,
        ),
        patch.object(_failure_cleanup, "_delete_child_session", delete_child),
    ):
        await _failure_cleanup.cleanup_failed_spawn(
            runner,
            "run-1",
            "spawn failed",
            handler,
            SimpleNamespace(),
            completion_registry=completion_registry,
            cleanup_isolation=True,
            task_manager=task_manager,
        )

    assert events == ["record-error", "terminalize", "cleanup-isolation", "delete-child"]
    assert terminalize_arguments == {
        "runner": runner,
        "run_id": "run-1",
        "terminal_reason": "spawn_rollback",
        "lifecycle_monitor": None,
        "completion_registry": completion_registry,
        "task_manager": task_manager,
        "message": "spawn failed",
    }


def _runner(
    start_result: object = None,
    *,
    current_status: str | None = None,
    start_error: Exception | None = None,
) -> SimpleNamespace:
    run_storage = MagicMock()
    if start_error is not None:
        run_storage.start.side_effect = start_error
    else:
        run_storage.start.return_value = start_result
    run_storage.get.return_value = (
        SimpleNamespace(status=current_status) if current_status is not None else None
    )
    return SimpleNamespace(run_storage=run_storage)


async def _start_run_or_cleanup(runner: SimpleNamespace) -> dict[str, object] | None:
    return await _failure_cleanup.start_run_or_cleanup(
        runner,
        "run-1",
        MagicMock(),
        MagicMock(),
        completion_registry=None,
        cleanup_isolation=True,
        task_manager=None,
        child_session_id="child-1",
        pid=4242,
        terminal_id="terminal-1",
    )


@pytest.mark.asyncio
async def test_start_cas_win_returns_success_without_cleanup() -> None:
    runner = _runner(SimpleNamespace(id="run-1", status="running"))

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result is None
    cleanup.assert_not_awaited()
    runner.run_storage.get.assert_not_called()


@pytest.mark.asyncio
async def test_lost_cas_with_running_run_treats_hook_win_as_success() -> None:
    """H4: SessionStart hook won the start race — no cleanup, terminal survives."""
    runner = _runner(None, current_status="running")

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result is None
    cleanup.assert_not_awaited()
    runner.run_storage.get.assert_called_once_with("run-1")


@pytest.mark.asyncio
async def test_lost_cas_with_non_running_run_cleans_up_and_reports_error() -> None:
    runner = _runner(None, current_status="cancelled")

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result == {
        "success": False,
        "error": "Agent run was no longer pending after spawn",
        "run_id": "run-1",
        "child_session_id": "child-1",
    }
    cleanup.assert_awaited_once()
    assert cleanup.await_args is not None
    assert cleanup.await_args.args[1] == "run-1"
    assert cleanup.await_args.kwargs == {
        "completion_registry": None,
        "cleanup_isolation": True,
        "task_manager": None,
        "child_session_id": "child-1",
        "pid": 4242,
        "terminal_id": "terminal-1",
        "cleanup_once": None,
        "attempt_terminal_known": True,
    }


@pytest.mark.asyncio
async def test_start_raising_cleans_up_and_reports_error() -> None:
    runner = _runner(start_error=RuntimeError("db down"))

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result == {
        "success": False,
        "error": "Failed to mark agent run run-1 as running: db down",
        "run_id": "run-1",
        "child_session_id": "child-1",
    }
    cleanup.assert_awaited_once()
    runner.run_storage.get.assert_not_called()
    assert cleanup.await_args is not None
    assert cleanup.await_args.kwargs["pid"] == 4242
    assert cleanup.await_args.kwargs["terminal_id"] == "terminal-1"


@pytest.mark.asyncio
async def test_terminate_does_not_sigkill_after_process_exits() -> None:
    sent: list[int] = []

    def fake_kill(_pid: int, sig: int) -> None:
        if sig == 0:
            raise ProcessLookupError
        sent.append(sig)

    cleanup_module = cast(Any, _failure_cleanup)
    with (
        patch.object(cleanup_module, "os") as mock_os,
        patch.object(cleanup_module.asyncio, "sleep", new_callable=AsyncMock),
        patch.object(cleanup_module, "_pid_starttime", side_effect=["stamp", None]),
    ):
        mock_os.kill.side_effect = fake_kill
        await _failure_cleanup._terminate_spawn_process(
            pid=4242,
            expected_starttime="stamp",
            terminal_manager=None,
            terminal_runtime_registry=None,
            terminal=None,
        )

    assert sent == [signal.SIGTERM]
    assert mock_os.kill.call_count >= 1
    assert signal.SIGKILL not in sent


@pytest.mark.asyncio
async def test_terminate_sigkills_only_when_pid_still_alive() -> None:
    sent: list[int] = []

    def fake_kill(_pid: int, sig: int) -> None:
        sent.append(sig)

    cleanup_module = cast(Any, _failure_cleanup)
    with (
        patch.object(cleanup_module, "os") as mock_os,
        patch.object(cleanup_module.asyncio, "sleep", new_callable=AsyncMock),
        patch.object(cleanup_module, "_pid_starttime", return_value="Mon Jan  1 00:00:00 2026"),
    ):
        mock_os.kill.side_effect = fake_kill
        await _failure_cleanup._terminate_spawn_process(
            pid=4242,
            expected_starttime="Mon Jan  1 00:00:00 2026",
            terminal_manager=None,
            terminal_runtime_registry=None,
            terminal=None,
        )

    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert mock_os.kill.call_count == 2
    assert sent[-1] == signal.SIGKILL


async def test_get_after_lost_start_race_cleans_up_on_storage_error() -> None:
    runner = _runner(None)
    runner.run_storage.get.side_effect = RuntimeError("read failed")

    with patch.object(_failure_cleanup, "cleanup_failed_spawn", AsyncMock()) as cleanup:
        result = await _start_run_or_cleanup(runner)

    assert result == {
        "success": False,
        "error": "Failed to read agent run run-1 after start conflict: read failed",
        "run_id": "run-1",
        "child_session_id": "child-1",
    }
    cleanup.assert_awaited_once()


class _HealthCaptureStorage:
    """In-memory capture store matching AgentRunStorage.replace_capture_slot."""

    def __init__(self, run: SimpleNamespace) -> None:
        self.db = object()
        self.run = run

    def get(self, _run_id: str) -> SimpleNamespace:
        return self.run

    def replace_capture_slot(
        self,
        _run_id: str,
        *,
        capture_id: str,
        expected_revision: int,
        marker: str,
        slot_content: str,
    ) -> SimpleNamespace:
        result = self.run.result or ""
        if self.run.capture_id is None:
            self.run.result = slot_content if not result else f"{result}\n\n{slot_content}"
        else:
            index = result.find(marker)
            self.run.result = result[:index] + slot_content if index >= 0 else slot_content
        self.run.capture_id = capture_id
        self.run.capture_revision = expected_revision + 1
        return self.run

    def fail(self, _run_id: str, error: str, **_kwargs: Any) -> SimpleNamespace:
        self.run.status = "error"
        self.run.error = error
        return self.run


@pytest.mark.asyncio
async def test_health_fail_persists_full_redacted_pane_for_get_agent_capture() -> None:
    from gobby.mcp_proxy.tools.agents import create_agents_registry
    from gobby.mcp_proxy.tools.spawn_agent._health import _deferred_spawn_health_check
    from gobby.utils.terminal_output import redact_terminal_output

    unique_head = "HEALTH_PANE_HEAD_7f3a9c"
    unique_tail = "HEALTH_PANE_TAIL_7f3a9c"
    pane = f"{unique_head}\n{'x' * 2048}\nsk-ABCDEFGHIJKLMNOPQRSTUV\n{unique_tail}"
    redacted = redact_terminal_output(pane.strip())
    assert unique_head in redacted
    assert unique_tail in redacted
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in redacted
    assert len(redacted) > 1024

    run = SimpleNamespace(
        id="run-health-1",
        status="running",
        result=None,
        error=None,
        capture_id=None,
        capture_revision=0,
        provider="claude",
        model="sonnet",
        tool_calls_count=0,
        turns_used=0,
        started_at=None,
        completed_at=None,
        child_session_id=None,
        terminal_reason=None,
        prompt="spawn",
        resume_metadata_json=None,
    )
    storage = _HealthCaptureStorage(run)
    terminal = SimpleNamespace(id="terminal-1", backend="native")
    terminal_manager = MagicMock()
    terminal_manager.get.return_value = terminal
    runner = SimpleNamespace(
        run_storage=storage,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=MagicMock(),
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health._terminal_is_live",
            new_callable=AsyncMock,
            return_value=(False, pane),
        ),
        patch(
            "gobby.agents.terminal_delivery.deliver_existing_terminal_run",
            new_callable=AsyncMock,
        ),
    ):
        await _deferred_spawn_health_check(
            runner,
            run_id=run.id,
            terminal_id=terminal.id,
            delay=0,
        )

    assert run.status == "error"
    assert run.capture_id
    assert run.error is not None
    assert f"capture_id={run.capture_id}" in run.error
    assert unique_head not in run.error
    assert unique_tail in run.error
    assert "[truncated]" in run.error

    query_runner = MagicMock()
    query_runner.get_run.return_value = run
    registry = create_agents_registry(query_runner)
    page = await registry.call(
        "get_agent_capture",
        {"run_id": run.id, "limit": len(redacted) + 32},
    )
    assert page["success"] is True
    assert page["content"] == redacted
    assert page["total_chars"] == len(redacted)
    assert page["content"][0] == redacted[0]
    assert page["content"][-1] == redacted[-1]
    assert unique_head in page["content"]
    assert unique_tail in page["content"]


class _RollbackCaptureStorage(_HealthCaptureStorage):
    """Capture store that also records the policy's termination intent."""

    def __init__(self, run: SimpleNamespace) -> None:
        super().__init__(run)
        self.intents: list[tuple[str, str | None]] = []

    def record_termination_intent(
        self,
        _run_id: str,
        *,
        action: str,
        reason: str | None = None,
        result_prefix: str | None = None,
    ) -> SimpleNamespace:
        self.intents.append((action, reason))
        return self.run


def _rollback_run() -> SimpleNamespace:
    return SimpleNamespace(
        id="run-rollback-1",
        status="pending",
        result=None,
        error=None,
        capture_id=None,
        capture_revision=0,
        child_session_id=None,
        pid=None,
        terminal_reason=None,
    )


class _RecordingTerminalRuntime:
    def __init__(self) -> None:
        self.terminations: list[tuple[object, float]] = []

    async def terminate(self, row: object, grace_seconds: float) -> None:
        self.terminations.append((row, grace_seconds))

    async def session_present(self, row: object) -> bool:
        return all(terminated is not row for terminated, _ in self.terminations)


class _RecordingRuntimeRegistry:
    def __init__(self, runtime: _RecordingTerminalRuntime) -> None:
        self.runtime = runtime
        self.resolved_backends: list[str] = []

    def resolve(self, backend: str) -> _RecordingTerminalRuntime:
        self.resolved_backends.append(backend)
        return self.runtime


_ATTEMPT: dict[str, Any] = {
    "attempt_generation": 3,
    "attempt_started_at": datetime(2026, 9, 29, 12, 0),
}


class _RecordingTerminalManager:
    def __init__(self) -> None:
        self.transitions: list[tuple[str, str, dict[str, Any]]] = []

    def fail_pending_attempt(self, terminal_id: str, **attempt: Any) -> None:
        self.transitions.append(("fail_pending_attempt", terminal_id, attempt))

    def mark_exited_attempt(self, terminal_id: str, **attempt: Any) -> None:
        self.transitions.append(("mark_exited_attempt", terminal_id, attempt))


@pytest.mark.asyncio
async def test_spawn_rollback_captures_before_terminating_runtime() -> None:
    run = _rollback_run()
    storage = _RollbackCaptureStorage(run)
    events: list[str] = []
    terminal = SimpleNamespace(
        id="terminal-1",
        backend="native",
        state="pending",
        spawn_key="gobby-rollback",
        **_ATTEMPT,
    )
    runtime = MagicMock()
    runtime.is_live = AsyncMock(return_value=True)

    async def capture(_row: object) -> SimpleNamespace:
        events.append("capture")
        return SimpleNamespace(text="spawn stderr: provider refused the lease")

    async def terminate(_row: object, _grace_seconds: float) -> None:
        events.append("terminate")

    runtime.snapshot_full = capture
    runtime.terminate = terminate
    runtime.session_present = AsyncMock(return_value=False)
    runtime_registry = MagicMock()
    runtime_registry.resolve.return_value = runtime
    terminal_manager = MagicMock()

    await _failure_cleanup._terminate_spawn_process(
        run_storage=storage,
        run_id=run.id,
        pid=None,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=runtime_registry,
        terminal=terminal,
    )

    assert events == ["capture", "terminate"]
    assert storage.intents == [("cancel", "spawn_rollback")]
    assert run.capture_id is not None
    assert "provider refused the lease" in (run.result or "")
    assert run.status == "pending"
    terminal_manager.fail_pending_attempt.assert_called_once_with(terminal.id, **_ATTEMPT)


@pytest.mark.asyncio
async def test_spawn_rollback_without_run_row_still_terminates_runtime() -> None:
    terminal = SimpleNamespace(
        id="terminal-1",
        backend="native",
        state="live",
        spawn_key="native-orphan",
        **_ATTEMPT,
    )
    runtime = _RecordingTerminalRuntime()
    runtime_registry = _RecordingRuntimeRegistry(runtime)
    terminal_manager = _RecordingTerminalManager()

    await _failure_cleanup._terminate_spawn_process(
        run_storage=None,
        run_id="run-missing",
        pid=None,
        terminal_manager=terminal_manager,
        terminal_runtime_registry=runtime_registry,
        terminal=terminal,
    )

    assert runtime_registry.resolved_backends == ["native"]
    assert runtime.terminations == [(terminal, 0.2)]
    assert terminal_manager.transitions == [("mark_exited_attempt", terminal.id, _ATTEMPT)]


@pytest.mark.asyncio
async def test_cleanup_terminates_via_runtime_and_settles_row() -> None:
    for state, transition in (("pending", "fail_pending_attempt"), ("live", "mark_exited_attempt")):
        terminal = SimpleNamespace(
            id=f"terminal-{state}",
            backend="native",
            state=state,
            spawn_key=f"spawn-{state}",
            **_ATTEMPT,
        )
        runtime = _RecordingTerminalRuntime()
        runtime_registry = _RecordingRuntimeRegistry(runtime)
        terminal_manager = _RecordingTerminalManager()

        await _failure_cleanup._terminate_spawn_process(
            run_storage=None,
            run_id=f"run-{state}",
            pid=None,
            terminal_manager=terminal_manager,
            terminal_runtime_registry=runtime_registry,
            terminal=terminal,
        )

        assert runtime_registry.resolved_backends == ["native"]
        assert runtime.terminations == [(terminal, 0.2)]
        assert terminal_manager.transitions == [(transition, terminal.id, _ATTEMPT)]


_SECRET = "sk-synthetic-secret-marker"


class _Runs:
    """Run storage whose row is already gone, so cleanup terminates the runtime directly."""

    def __init__(self, db: object | None = None) -> None:
        self.db = db
        self.errors: list[str] = []

    def record_spawn_error(self, _run_id: str, error: str) -> None:
        self.errors.append(error)

    def get(self, _run_id: str) -> None:
        return None


class _Isolation:
    def __init__(self) -> None:
        self.removed = 0

    async def cleanup_environment(self, _spawn_config: object) -> None:
        self.removed += 1


@dataclass
class _StickyRuntime(FakeRuntime):
    """A runtime whose terminate returns while the session stays present."""

    backend: Literal["tmux", "native"] = "native"

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        del grace_seconds
        self.terminate_started.set()
        self.killed.append(terminal.id)


@dataclass
class _UnprovableRuntime(FakeRuntime):
    """A native runtime that cannot prove a pid-less pending terminal dead."""

    backend: Literal["tmux", "native"] = "native"

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        del grace_seconds
        self.terminate_started.set()
        raise RuntimeError(f"no recorded process to prove {_SECRET}")


def _row(state: str) -> Terminal:
    # A committed native terminal is addressed by its host terminal id on the
    # host epoch that spawned it.
    row = replace(
        make_memory_terminal(backend="native"),
        host_epoch="epoch",
        locator={"host_terminal_id": "ht-1"},
        locator_key=native_locator_key("epoch", "ht-1"),
    )
    if state == "pending":
        return replace(row, state="pending", locator=None, locator_key=None)
    return replace(row, state=state)


async def _cleanup(
    store: MemoryTerminalStore,
    runtime: FakeRuntime,
    terminal_id: str,
    handler: _Isolation,
    *,
    cleanup_isolation: bool = True,
    prior_attempt: tuple[int, datetime] | None = None,
    terminalize: Callable[..., Awaitable[bool]] | None = None,
    child_sessions: object | None = None,
) -> None:
    runner = SimpleNamespace(
        run_storage=_Runs(),
        terminal_manager=store,
        terminal_runtime_registry=runtime_registry(runtime),
        agent_lifecycle_monitor=None,
        child_session_manager=SimpleNamespace(_storage=child_sessions),
    )
    with patch(
        "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
        terminalize or AsyncMock(return_value=True),
    ):
        await _failure_cleanup.cleanup_failed_spawn(
            runner,
            "run-1",
            "spawn failed",
            handler,
            SimpleNamespace(),
            completion_registry=None,
            cleanup_isolation=cleanup_isolation,
            task_manager=None,
            child_session_id=None if child_sessions is None else "child-1",
            terminal_id=terminal_id,
            prior_attempt=prior_attempt,
        )


async def test_failed_kill_orphans_and_keeps_isolation() -> None:
    native_pending = _row("pending")
    store = MemoryTerminalStore(native_pending)
    unprovable = _UnprovableRuntime()
    handler = _Isolation()
    await _cleanup(store, unprovable, native_pending.id, handler)
    assert unprovable.terminate_started.is_set()
    assert store.rows[native_pending.id].state == "pending"
    assert handler.removed == 0

    sticky_pending = _row("pending")
    store = MemoryTerminalStore(sticky_pending)
    sticky = _StickyRuntime(live_keys={str(sticky_pending.spawn_key)})
    await _cleanup(store, sticky, sticky_pending.id, handler)
    assert sticky.killed == [sticky_pending.id]
    assert store.rows[sticky_pending.id].state == "pending"
    assert handler.removed == 0

    held = _row("pending")
    store = MemoryTerminalStore(held)
    untouched = FakeRuntime(backend="native")
    in_doubt_spawns.claim(held.id)
    try:
        await _cleanup(store, untouched, held.id, handler)
    finally:
        in_doubt_spawns.release(held.id)
    assert not untouched.terminate_started.is_set()
    assert store.rows[held.id].state == "pending"
    assert handler.removed == 0

    live = _row("live")
    store = MemoryTerminalStore(live)
    sticky = _StickyRuntime(live_keys={str(live.spawn_key)})
    await _cleanup(store, sticky, live.id, handler)
    orphan = store.get(live.id)
    assert orphan is not None
    assert orphan.state == "orphaned"
    assert handler.removed == 0

    for state in ("pending", "live"):
        proven = _row(state)
        store = MemoryTerminalStore(proven)
        await _cleanup(store, FakeRuntime(backend="native"), proven.id, handler)
        assert store.rows[proven.id].state == "exited"
    assert handler.removed == 2


async def _run_deferred(steps: list[Any], terminalize: AsyncMock) -> None:
    with patch(
        "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run", terminalize
    ):
        for step in steps:
            await step()


@pytest.mark.parametrize("proven", [True, False])
async def test_held_terminal_defers_rollback_and_isolation_to_owner(proven: bool) -> None:
    held = _row("pending")
    store = MemoryTerminalStore(held)
    handler = _Isolation()
    terminalize = AsyncMock(return_value=True)
    in_doubt_spawns.claim(held.id)
    try:
        await _cleanup(
            store, FakeRuntime(backend="native"), held.id, handler, terminalize=terminalize
        )
        # Run terminalization would exit the held row, so it waits for the owner.
        terminalize.assert_not_awaited()
        assert handler.removed == 0
        assert store.rows[held.id].state == "pending"
    finally:
        deferred = in_doubt_spawns.release(held.id, proven=proven)

    await _run_deferred(deferred, terminalize)

    # A kept orphan still rolls back the run; only a proven exit removes isolation.
    terminalize.assert_awaited_once()
    assert handler.removed == int(proven)


class _ChildSessions:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete(self, child_session_id: str) -> None:
        self.deleted.append(child_session_id)


async def _reap_with_concurrent_cleanup(
    store: MemoryTerminalStore,
    row: Terminal,
    *,
    absent: bool,
    settle: AbstractContextManager[object],
    handler: _Isolation,
    children: _ChildSessions,
    terminalize: AsyncMock,
) -> asyncio.Task[bool]:
    """Reap ``row`` once while a failed spawn's cleanup lands mid-reap."""
    entered, resume = asyncio.Event(), asyncio.Event()

    async def first_absence(*_args: object) -> bool:
        entered.set()
        await resume.wait()
        return absent

    registry = runtime_registry(FakeRuntime(backend="native"))
    manager = cast(TerminalManager, store)
    with (
        patch.object(spawn_executor, "_stale_pending_absent", first_absence),
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run", terminalize
        ),
        settle,
    ):
        reaping = asyncio.create_task(spawn_executor._reap_stale_row(manager, registry, row))
        await entered.wait()
        # Cleanup lands while the reaper holds the id, so its steps wait on that claim.
        await _cleanup(
            store,
            FakeRuntime(backend="native"),
            row.id,
            handler,
            terminalize=terminalize,
            child_sessions=children,
        )
        resume.set()
        await asyncio.wait([reaping])
    return reaping


@pytest.mark.parametrize("first_reap", ["absence_unproven", "settle_raises"])
async def test_unsettled_reap_keeps_deferred_cleanup_for_the_proven_reap(first_reap: str) -> None:
    row = _row("pending")
    store = MemoryTerminalStore(row)
    registry = runtime_registry(FakeRuntime(backend="native"))
    manager = cast(TerminalManager, store)
    handler = _Isolation()
    children = _ChildSessions()
    terminalize = AsyncMock(return_value=True)
    settle_failure: AbstractContextManager[object] = nullcontext()
    if first_reap == "settle_raises":
        settle_failure = patch.object(
            store, "fail_pending_attempt", side_effect=RuntimeError("settle failed")
        )

    reaping = await _reap_with_concurrent_cleanup(
        store,
        row,
        absent=first_reap != "absence_unproven",
        settle=settle_failure,
        handler=handler,
        children=children,
        terminalize=terminalize,
    )
    if first_reap == "settle_raises":
        with pytest.raises(RuntimeError, match="settle failed"):
            reaping.result()
    else:
        assert reaping.result() is False

    # The unsettled reap ran nothing and stays suspended on its attempt with the steps,
    # so no other owner (a placed retry, another attempt) can claim and settle the row.
    assert in_doubt_spawns.holds(row.id)
    assert not in_doubt_spawns.claim(row.id)
    assert not in_doubt_spawns.claim(
        row.id, attempt=(row.attempt_generation + 1, row.attempt_started_at)
    )
    assert store.rows[row.id].state == "pending"
    terminalize.assert_not_awaited()
    assert (handler.removed, children.deleted) == (0, [])

    with (
        patch.object(spawn_executor, "_stale_pending_absent", AsyncMock(return_value=True)),
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            terminalize,
        ),
    ):
        assert await spawn_executor._reap_stale_row(manager, registry, row) is True
        # The settled row is no longer reaped, so nothing runs twice.
        assert await spawn_executor._reap_stale_row(manager, registry, row) is False

    assert store.rows[row.id].state == "exited"
    terminalize.assert_awaited_once()
    assert (handler.removed, children.deleted) == (1, ["child-1"])
    assert not in_doubt_spawns.holds(row.id)


async def test_cas_missed_reap_releases_a_row_that_left_its_attempt() -> None:
    row = _row("pending")
    store = MemoryTerminalStore(row)
    handler = _Isolation()
    children = _ChildSessions()
    terminalize = AsyncMock(return_value=True)

    def exited_under_the_reaper(terminal_id: str, **_attempt: object) -> None:
        # Another path exits the row between the reaper's read and its settle.
        store.mark_exited(terminal_id)

    reaping = await _reap_with_concurrent_cleanup(
        store,
        row,
        absent=True,
        settle=patch.object(store, "fail_pending_attempt", side_effect=exited_under_the_reaper),
        handler=handler,
        children=children,
        terminalize=terminalize,
    )

    assert reaping.result() is False
    assert store.rows[row.id].state == "exited"
    # The death proof remains valid after a CAS miss; compensation still runs.
    assert not in_doubt_spawns.holds(row.id)
    terminalize.assert_awaited_once()
    assert (handler.removed, children.deleted) == (1, ["child-1"])


@pytest.mark.parametrize("moved_to", ["exited", "missing", "new_pending", "new_live"])
async def test_sweep_reaches_suspended_cleanup_after_the_row_moves_on(moved_to: str) -> None:
    row = _row("pending")
    store = MemoryTerminalStore(row)
    handler = _Isolation()
    children = _ChildSessions()

    async def rollback_while_protected(**_kwargs: object) -> bool:
        assert in_doubt_spawns.holds(row.id)
        return True

    terminalize = AsyncMock(side_effect=rollback_while_protected)
    reaping = await _reap_with_concurrent_cleanup(
        store,
        row,
        absent=False,
        settle=nullcontext(),
        handler=handler,
        children=children,
        terminalize=terminalize,
    )
    assert reaping.result() is False
    if moved_to == "missing":
        del store.rows[row.id]
    elif moved_to == "exited":
        store.mark_exited(row.id)
    else:
        moved = replace(row, attempt_generation=row.attempt_generation + 1)
        if moved_to == "new_live":
            # A committed native attempt carries its host terminal locator.
            moved = replace(
                moved,
                state="live",
                locator={"host_terminal_id": "ht-2"},
                locator_key=native_locator_key("epoch", "ht-2"),
            )
        store.rows[row.id] = moved
    moved_row = store.get(row.id)
    registry = runtime_registry(FakeRuntime(backend="native"))
    manager = cast(TerminalManager, store)
    probe = AsyncMock(return_value=False)
    with patch.object(spawn_executor, "_stale_pending_absent", probe):
        await spawn_executor.reap_stale_pending_terminals(manager, registry, in_doubt_seconds=0)
    # Moving the row is no proof that the old process died; keep every step.
    assert in_doubt_spawns.holds(row.id)
    terminalize.assert_not_awaited()
    assert (handler.removed, children.deleted) == (0, [])
    probe.assert_awaited_once()
    assert probe.await_args is not None
    assert probe.await_args.args[1].attempt_generation == row.attempt_generation
    assert probe.await_args.kwargs == {"terminate": False}

    probe.reset_mock(return_value=True)
    probe.return_value = True
    with (
        patch.object(spawn_executor, "_stale_pending_absent", probe),
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run", terminalize
        ),
    ):
        await spawn_executor.reap_stale_pending_terminals(manager, registry, in_doubt_seconds=0)
    assert not in_doubt_spawns.holds(row.id)
    terminalize.assert_awaited_once()
    assert (handler.removed, children.deleted) == (1, ["child-1"])
    assert store.get(row.id) == moved_row
    with patch.object(spawn_executor, "_stale_pending_absent", probe):
        await spawn_executor.reap_stale_pending_terminals(manager, registry, in_doubt_seconds=1e12)
    probe.assert_awaited_once()
    terminalize.assert_awaited_once()
    assert (handler.removed, children.deleted) == (1, ["child-1"])


async def test_held_terminal_defers_isolation_to_owner() -> None:
    reused = _row("pending")
    handler = _Isolation()
    terminalize = AsyncMock(return_value=True)
    in_doubt_spawns.claim(reused.id)
    try:
        await _cleanup(
            MemoryTerminalStore(reused),
            FakeRuntime(backend="native"),
            reused.id,
            handler,
            cleanup_isolation=False,
            terminalize=terminalize,
        )
    finally:
        deferred = in_doubt_spawns.release(reused.id)
    await _run_deferred(deferred, terminalize)
    terminalize.assert_awaited_once()
    assert handler.removed == 0

    # The owner settles and releases while cleanup is still running, so the
    # late isolation step finds no claim and decides from the row it left.
    # (label, the row the owner leaves or None for no row, whether the failed
    # result's prior_attempt is the row's pair, whether isolation is removed)
    cases: list[tuple[str, str | None, bool, bool]] = [
        ("exited", "exited", False, True),
        ("no row", None, False, True),
        ("rolled-back bump", "pending", True, True),
        ("pending under another pair", "pending", False, False),
        ("live under another pair", "live", False, False),
        ("orphaned under another pair", "orphaned", False, False),
    ]
    for label, settled_state, carries_prior, removes in cases:
        row = _row("pending")
        store = MemoryTerminalStore(row)
        handler = _Isolation()
        prior_generation = row.attempt_generation if carries_prior else row.attempt_generation - 1
        prior = (prior_generation, row.attempt_started_at)

        def owner_settles(
            _run_id: str | None,
            _store: MemoryTerminalStore = store,
            _row: Terminal = row,
            _state: str | None = settled_state,
        ) -> None:
            in_doubt_spawns.release(_row.id)
            if _state is None:
                _store.rows.pop(_row.id)
            else:
                _store.rows[_row.id] = replace(_row, state=_state)

        # The owner settles after cleanup's held kill was skipped and before its
        # rollback and isolation steps look for the claim.
        in_doubt_spawns.claim(row.id)
        try:
            with patch.object(_failure_cleanup, "_forget_spawn_run", owner_settles):
                await _cleanup(
                    store, FakeRuntime(backend="native"), row.id, handler, prior_attempt=prior
                )
        finally:
            in_doubt_spawns.release(row.id)
        assert handler.removed == int(removes), label


_CLEANUP_PHASES = (
    "read_run",
    "record_error",
    "terminate",
    "forget_run",
    "terminalize_run",
    "runtime_state",
    "isolation",
    "delete_child_session",
)


@pytest.mark.parametrize("failing", _CLEANUP_PHASES)
async def test_cleanup_steps_are_independent(
    failing: str, caplog: pytest.LogCaptureFixture
) -> None:
    ran: list[str] = []

    def step(phase: str) -> None:
        ran.append(phase)
        if phase == failing:
            raise RuntimeError(f"{phase} leaked {_SECRET}")

    class Runs(_Runs):
        def record_spawn_error(self, _run_id: str, _error: str) -> None:
            step("record_error")

        def get(self, _run_id: str) -> None:
            step("read_run")

    async def terminate(**_kwargs: Any) -> bool:
        step("terminate")
        return True

    def forget(_run_id: str | None) -> None:
        step("forget_run")

    async def terminalize(**_kwargs: Any) -> bool:
        step("terminalize_run")
        return True

    def runtime_state(*_args: Any, **_kwargs: Any) -> None:
        step("runtime_state")

    # The isolation handler and the session storage fail inside the real helpers.
    class Handler:
        async def cleanup_environment(self, _spawn_config: object) -> None:
            step("isolation")

    class Sessions:
        def delete(self, _child_session_id: str) -> None:
            step("delete_child_session")

    connection = SimpleNamespace(execute=lambda *_args: None)
    db = SimpleNamespace(transaction=lambda: nullcontext(connection))
    runner = SimpleNamespace(
        run_storage=Runs(db=db),
        agent_lifecycle_monitor=None,
        child_session_manager=SimpleNamespace(_storage=Sessions()),
    )
    caplog.set_level(logging.WARNING, logger=_failure_cleanup.__name__)
    with (
        patch.object(_failure_cleanup, "_terminate_spawn_process", terminate),
        patch.object(_failure_cleanup, "_forget_spawn_run", forget),
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            terminalize,
        ),
        patch("gobby.agents.runtime_cleanup.cleanup_agent_runtime_state", runtime_state),
    ):
        await _failure_cleanup.cleanup_failed_spawn(
            runner,
            "run-7",
            "spawn failed",
            Handler(),
            SimpleNamespace(),
            completion_registry=None,
            cleanup_isolation=True,
            task_manager=None,
            child_session_id="child-7",
        )

    # A failed terminate proves nothing, so isolation is kept for it.
    expected = [
        phase for phase in _CLEANUP_PHASES if failing != "terminate" or phase != "isolation"
    ]
    assert ran == expected
    failures = [record for record in caplog.records if "RuntimeError" in record.getMessage()]
    assert len(failures) == 1
    message = failures[0].getMessage()
    assert failing in message
    assert "run-7" in message
    assert _SECRET not in caplog.text


@pytest.mark.parametrize("run_id", ["run-9", None])
async def test_created_isolation_cleanup_failure_logs_phase_run_and_terminal(
    run_id: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    class Handler:
        async def cleanup_environment(self, _spawn_config: object) -> None:
            raise RuntimeError(f"isolation leaked {_SECRET}")

    caplog.set_level(logging.WARNING, logger=_failure_cleanup.__name__)
    await _failure_cleanup.cleanup_created_isolation(
        Handler(), SimpleNamespace(), cleanup=True, run_id=run_id
    )

    assert [record.getMessage() for record in caplog.records] == [
        f"Spawn cleanup step isolation failed for run {run_id} (terminal None): RuntimeError"
    ]
    assert _SECRET not in caplog.text


async def test_cleanup_survives_cancellation() -> None:
    ran: list[str] = []
    terminalize_entered = asyncio.Event()
    terminalize_go = asyncio.Event()
    isolation_entered = asyncio.Event()
    isolation_go = asyncio.Event()

    async def terminalize(**_kwargs: Any) -> bool:
        ran.append("terminalize_run")
        terminalize_entered.set()
        await terminalize_go.wait()
        return True

    class Handler:
        async def cleanup_environment(self, _spawn_config: object) -> None:
            ran.append("isolation")
            isolation_entered.set()
            await isolation_go.wait()

    def delete_child(*_args: Any, **_kwargs: Any) -> None:
        ran.append("delete_child_session")

    once = _failure_cleanup.SpawnCleanupOnce()
    runs = _Runs()

    def cleanup() -> Coroutine[Any, Any, None]:
        return _failure_cleanup.cleanup_failed_spawn(
            SimpleNamespace(run_storage=runs, agent_lifecycle_monitor=None),
            "run-1",
            "spawn failed",
            Handler(),
            SimpleNamespace(),
            completion_registry=None,
            cleanup_isolation=True,
            task_manager=None,
            cleanup_once=once,
        )

    with (
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            terminalize,
        ),
        patch.object(_failure_cleanup, "_delete_child_session", delete_child),
    ):
        caller = asyncio.create_task(cleanup())
        await asyncio.wait_for(terminalize_entered.wait(), 5)
        caller.cancel("first")
        await asyncio.sleep(0)
        terminalize_go.set()
        await asyncio.wait_for(isolation_entered.wait(), 5)
        caller.cancel("second")
        await asyncio.sleep(0)
        isolation_go.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await caller
        assert raised.value.args == ("first",)
        assert ran == ["terminalize_run", "isolation", "delete_child_session"]
        assert runs.errors == ["spawn failed"]

        late = asyncio.create_task(cleanup())
        late.cancel("late")
        with pytest.raises(asyncio.CancelledError):
            await late
        await cleanup()
    assert ran == ["terminalize_run", "isolation", "delete_child_session"]
    assert runs.errors == ["spawn failed"]


async def test_cleanup_runs_once_per_attempt() -> None:
    for raised_after_cleanup in (RuntimeError("finalize broke"), asyncio.CancelledError()):
        runs = _Runs()
        terminalize = AsyncMock(return_value=True)
        once = _failure_cleanup.SpawnCleanupOnce()
        runner = SimpleNamespace(run_storage=runs, agent_lifecycle_monitor=None)

        async def finalize(
            *,
            spawn_result: object,
            _runner: SimpleNamespace = runner,
            _once: _failure_cleanup.SpawnCleanupOnce = once,
            _error: BaseException = raised_after_cleanup,
        ) -> dict[str, Any]:
            await _failure_cleanup.cleanup_failed_spawn(
                _runner,
                "run-1",
                "liveness failed",
                SimpleNamespace(),
                SimpleNamespace(),
                completion_registry=None,
                cleanup_isolation=False,
                task_manager=None,
                cleanup_once=_once,
            )
            raise _error

        phase = SpawnPhase(
            runner=runner,
            run_id="run-1",
            spawn_request=SimpleNamespace(),
            execute=AsyncMock(return_value=SimpleNamespace(pid=None)),
            finalize=finalize,
            handler=SimpleNamespace(),
            spawn_config=SimpleNamespace(),
            completion_registry=None,
            cleanup_isolation=False,
            task_manager=None,
            child_session_id=None,
            spawn_identity={"run_id": "run-1"},
            reasoning=SimpleNamespace(to_dict=dict),
            cleanup_once=once,
        )
        with patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            terminalize,
        ):
            if isinstance(raised_after_cleanup, asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await phase.execute_phase()
            else:
                result = await phase.execute_phase()
                assert result["success"] is False
        assert terminalize.await_count == 1
        assert runs.errors == ["liveness failed"]
