"""Tests for thread-offloaded agent run completion helpers."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.agents.run_completion as run_completion

pytestmark = pytest.mark.unit


def test_workflow_completion_notification_includes_terminal_task_state() -> None:
    task_manager = MagicMock()
    task_manager.get_task.return_value = SimpleNamespace(
        id="22222222-2222-4222-8222-222222222222",
        seq_num=21617,
        claimed_by_session_id=None,
        is_escalated=True,
        escalation_reason="Coordinator restart required.",
        commits=["abc123", "def456"],
    )

    with patch.object(run_completion, "LocalTaskManager", return_value=task_manager):
        result, message = run_completion.build_workflow_completion_notification(
            MagicMock(),
            "run-123",
            "backend-developer-steps",
            "22222222-2222-4222-8222-222222222222",
        )

    assert result == {
        "status": "success",
        "run_id": "run-123",
        "via": "workflow_terminate",
        "workflow": "backend-developer-steps",
        "task_ref": "#21617",
        "claimed_by": None,
        "is_escalated": True,
        "escalation_reason": "Coordinator restart required.",
        "linked_commits": ["abc123", "def456"],
    }
    assert message == (
        "Agent run-123 completed via workflow terminate; task_ref=#21617; "
        "claimed_by=none; is_escalated=true; "
        'escalation_reason="Coordinator restart required."; '
        'linked_commits=["abc123","def456"]'
    )


@pytest.mark.asyncio
async def test_complete_and_notify_agent_run_offloads_complete_run() -> None:
    runner = MagicMock()
    runner.complete_run.return_value = True
    runner.get_run.return_value = SimpleNamespace(status="success")
    completion_registry = MagicMock()
    completion_registry.get_result.return_value = None
    completion_registry.notify = AsyncMock(return_value={})
    to_thread_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(
        func: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    run_completion.configure_terminal_delivery_offload(async_offload=fake_to_thread)
    try:
        completed = await run_completion.complete_and_notify_agent_run(
            runner,
            "run-123",
            completion_registry=completion_registry,
            notify_result={"status": "success"},
        )
    finally:
        run_completion.reset_terminal_delivery_offload()

    assert completed is True
    assert to_thread_calls[0] == (
        runner.complete_run,
        ("run-123",),
        {"result": None},
    )
    assert to_thread_calls[1][0].__name__ == "read_terminal_run"
    assert to_thread_calls[1][1:] == ((), {})
    runner.run_storage.db.bounded_transaction.assert_called_once_with()
    completion_registry.notify.assert_awaited_once_with(
        "run-123",
        result={"status": "success", "run_id": "run-123"},
        message="",
        durable_subscriber_count=0,
    )
    completion_registry.cleanup.assert_called_once_with("run-123")


@pytest.mark.asyncio
async def test_complete_and_notify_normalizes_a_copy_of_notify_result() -> None:
    runner = MagicMock()
    runner.complete_run.return_value = True
    runner.get_run.return_value = SimpleNamespace(status="success")
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock(return_value={})
    notify_result = {"status": "success", "run_id": "stale-run", "error": None}

    completed = await run_completion.complete_and_notify_agent_run(
        runner,
        "run-current",
        completion_registry=completion_registry,
        notify_result=notify_result,
    )

    assert completed is True
    assert notify_result == {"status": "success", "run_id": "stale-run", "error": None}
    completion_registry.notify.assert_awaited_once_with(
        "run-current",
        result={"status": "success", "run_id": "run-current"},
        message="",
        durable_subscriber_count=0,
    )


async def test_complete_and_notify_settles_delivery_before_cancellation() -> None:
    runner = MagicMock()
    runner.complete_run.return_value = True
    runner.get_run.return_value = SimpleNamespace(status="success")
    completion_registry = MagicMock()
    completion_registry.notify = AsyncMock(return_value={})
    started = asyncio.Event()
    release = asyncio.Event()

    async def offload(func, *args, **kwargs):
        if func is runner.complete_run:
            started.set()
            await release.wait()
        return func(*args, **kwargs)

    run_completion.configure_terminal_delivery_offload(async_offload=offload)
    try:
        owner = asyncio.create_task(
            run_completion.complete_and_notify_agent_run(
                runner,
                "run-cancelled",
                completion_registry=completion_registry,
                notify_result={"status": "success"},
            )
        )
        await started.wait()
        owner.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            await owner
    finally:
        run_completion.reset_terminal_delivery_offload()

    completion_registry.notify.assert_awaited_once()


def _dirty_git_checkout(path: Path) -> None:
    path.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=path,
        check=True,
        timeout=10,
    )
    tracked = path / "tracked.py"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=path, check=True, timeout=10)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gobby Tests",
            "-c",
            "user.email=gobby-tests@example.com",
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "initial",
        ],
        cwd=path,
        check=True,
        timeout=10,
    )
    tracked.write_text("after\n", encoding="utf-8")
    (path / "untracked.py").write_text("new\n", encoding="utf-8")


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.run_storage.db = MagicMock()
    return runner


def test_failed_isolated_run_reports_checkout_dirt_when_attribution_read_fails(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "task-worktree"
    _dirty_git_checkout(checkout)
    runner = _runner()
    run = SimpleNamespace(
        task_id="task-id",
        child_session_id="session-id",
        worktree_id="worktree-id",
        clone_id=None,
        status="error",
    )
    variable_reads: list[str] = []
    worktree_reads: list[str] = []

    def get_variables(session_id: str) -> dict[str, object]:
        variable_reads.append(session_id)
        raise RuntimeError("session attribution unavailable after restart")

    def get_worktree(worktree_id: str) -> SimpleNamespace:
        worktree_reads.append(worktree_id)
        return SimpleNamespace(worktree_path=str(checkout))

    variable_manager = SimpleNamespace(get_variables=get_variables)
    worktree_manager = SimpleNamespace(get=get_worktree)

    with (
        patch.object(
            run_completion,
            "SessionVariableManager",
            return_value=variable_manager,
        ),
        patch.object(
            run_completion,
            "LocalWorktreeManager",
            return_value=worktree_manager,
        ),
    ):
        dirty_paths = run_completion.agent_run_task_dirty_paths(
            runner.run_storage.db,
            runner._session_manager,
            run,
        )

    assert dirty_paths == ["tracked.py", "untracked.py"]
    assert variable_reads == ["session-id"]
    assert worktree_reads == ["worktree-id"]


def test_shared_checkout_without_attribution_does_not_claim_all_dirty_paths(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "shared-checkout"
    _dirty_git_checkout(checkout)
    runner = _runner()
    runner._session_manager.get.return_value = object()
    run = SimpleNamespace(
        task_id="task-id",
        child_session_id="session-id",
        worktree_id=None,
        clone_id=None,
        status="error",
    )
    variable_manager = MagicMock()
    variable_manager.get_variables.return_value = {}

    with (
        patch.object(
            run_completion,
            "SessionVariableManager",
            return_value=variable_manager,
        ),
        patch.object(
            run_completion,
            "resolve_session_checkout_root",
            return_value=checkout,
        ),
    ):
        dirty_paths = run_completion.agent_run_task_dirty_paths(
            runner.run_storage.db,
            runner._session_manager,
            run,
        )

    assert dirty_paths == []
