"""Tests for start_run_or_cleanup lost-CAS tolerance on the fresh-spawn path."""

from __future__ import annotations

import signal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import SpawnConfig, WorktreeIsolationHandler
from gobby.mcp_proxy.tools.spawn_agent import _failure_cleanup
from gobby.worktrees.git import WorktreeGitManager

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
        terminal_type="none",
        pid=None,
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
        tmux_session_name=None,
        tmux_socket_name=None,
        tmux_socket_path=None,
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

    async def cleanup_isolation(*_args: Any, **_kwargs: Any) -> None:
        events.append("cleanup-isolation")

    def delete_child(*_args: Any, **_kwargs: Any) -> None:
        events.append("delete-child")

    run_storage = RunStorage()
    runner = SimpleNamespace(run_storage=run_storage, agent_lifecycle_monitor=None)
    handler = SimpleNamespace()
    completion_registry = object()
    task_manager = object()
    with (
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.terminalize_cancelled_agent_run",
            terminalize,
        ),
        patch.object(_failure_cleanup, "cleanup_created_isolation", cleanup_isolation),
        patch.object(_failure_cleanup, "_delete_child_session", delete_child),
    ):
        await _failure_cleanup.cleanup_failed_spawn(
            runner,
            "run-1",
            "spawn failed",
            handler,
            SimpleNamespace(),
            completion_registry=completion_registry,
            cleanup_isolation=False,
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
        tmux_session_name="spawn-session",
        tmux_socket_name="gobby",
        tmux_socket_path=None,
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
    """H4: SessionStart hook won the start race — no cleanup, tmux survives."""
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
        "tmux_session_name": "spawn-session",
        "tmux_socket_name": "gobby",
        "tmux_socket_path": None,
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
    assert cleanup.await_args.kwargs["tmux_session_name"] == "spawn-session"


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
            tmux_session_name=None,
            tmux_socket_name=None,
            tmux_socket_path=None,
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
            tmux_session_name=None,
            tmux_socket_name=None,
            tmux_socket_path=None,
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
    from gobby.mcp_proxy.tools.spawn_agent._health import _deferred_tmux_health_check
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
    runner = SimpleNamespace(run_storage=storage)

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._health._check_tmux_session_alive",
            new_callable=AsyncMock,
            return_value=(False, pane),
        ),
        patch(
            "gobby.agents.terminal_delivery.deliver_existing_terminal_run",
            new_callable=AsyncMock,
        ),
    ):
        await _deferred_tmux_health_check(
            runner,
            run_id=run.id,
            tmux_session_name="tmux-run",
            socket_name=None,
            socket_path=None,
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
        tmux_session_name="gobby-rollback",
        terminal_reason=None,
    )


@pytest.mark.asyncio
async def test_spawn_rollback_captures_pane_before_killing_tmux() -> None:
    run = _rollback_run()
    storage = _RollbackCaptureStorage(run)
    events: list[str] = []
    tmux = MagicMock()
    tmux.has_session = AsyncMock(return_value=True)

    async def capture(_name: str) -> str:
        events.append("capture")
        return "spawn stderr: provider refused the lease"

    async def kill(_name: str, *, missing_ok: bool = False) -> bool:
        events.append("kill")
        return True

    tmux.capture_full_pane = capture
    tmux.kill_session = kill

    with patch("gobby.agents.tmux.get_tmux_session_manager", return_value=tmux):
        await _failure_cleanup._terminate_spawn_process(
            run_storage=storage,
            run_id=run.id,
            pid=None,
            tmux_session_name=run.tmux_session_name,
            tmux_socket_name=None,
            tmux_socket_path=None,
        )

    assert events == ["capture", "kill"]
    assert storage.intents == [("cancel", "spawn_rollback")]
    assert run.capture_id is not None
    assert "provider refused the lease" in (run.result or "")
    assert run.status == "pending"


@pytest.mark.asyncio
async def test_spawn_rollback_without_run_row_still_kills_tmux() -> None:
    events: list[str] = []
    tmux = MagicMock()

    async def capture(_name: str) -> str:
        events.append("capture")
        return ""

    async def kill(name: str, *, missing_ok: bool = False) -> bool:
        events.append(f"kill:{name}:{missing_ok}")
        return True

    tmux.capture_full_pane = capture
    tmux.kill_session = kill

    with patch("gobby.agents.tmux.get_tmux_session_manager", return_value=tmux):
        await _failure_cleanup._terminate_spawn_process(
            run_storage=None,
            run_id="run-missing",
            pid=None,
            tmux_session_name="gobby-orphan",
            tmux_socket_name=None,
            tmux_socket_path=None,
        )

    assert events == ["kill:gobby-orphan:True"]
