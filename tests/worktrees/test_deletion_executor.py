"""Concurrency and cancellation tests for managed worktree deletion."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.storage.tasks import LocalTaskManager
from gobby.storage.worktrees import LocalWorktreeManager, Worktree
from gobby.worktrees.deletion import (
    DeletionSurface,
    WorktreeDeletionRequest,
    delete_worktree_transaction,
)
from gobby.worktrees.executor import DestructiveBoundary, WorktreeDeleteExecutor
from gobby.worktrees.git import WorktreeGitManager
from tests._timing import drain_asyncio_tasks, wait_for_async_condition

pytestmark = pytest.mark.unit


class _Storage:
    def __init__(self, rows: dict[str, Worktree]) -> None:
        self.rows = rows
        self.thread_ids: set[int] = set()
        self._lock = threading.Lock()

    def get(self, worktree_id: str) -> Worktree | None:
        with self._lock:
            self.thread_ids.add(threading.get_ident())
            return self.rows.get(worktree_id)

    def delete(self, worktree_id: str) -> bool:
        with self._lock:
            self.thread_ids.add(threading.get_ident())
            return self.rows.pop(worktree_id, None) is not None


class _Artifacts:
    def __init__(self, refs: set[str]) -> None:
        self.refs = refs
        self.thread_ids: set[int] = set()
        self._lock = threading.Lock()

    def clear_worktree_references(self, worktree_id: str) -> int:
        with self._lock:
            self.thread_ids.add(threading.get_ident())
            existed = worktree_id in self.refs
            self.refs.discard(worktree_id)
            return int(existed)


class _TaskManager:
    def __init__(self, refs: set[str]) -> None:
        self.artifacts = _Artifacts(refs)


class _BarrierGitManager:
    def __init__(self, worker_count: int) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.thread_ids: set[int] = set()
        self.active = 0
        self.max_active = 0
        self._worker_count = worker_count
        self._lock = threading.Lock()

    def get_worktree_status(self, _path: str) -> SimpleNamespace:
        return SimpleNamespace(has_uncommitted_changes=False)

    def delete_worktree(self, _path: str, **_kwargs: Any) -> SimpleNamespace:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.thread_ids.add(threading.get_ident())
            if self.active == self._worker_count:
                self.started.set()
        try:
            assert self.release.wait(timeout=2)
            return SimpleNamespace(success=True, message="deleted", error=None)
        finally:
            with self._lock:
                self.active -= 1


def _worktree(worktree_id: str, path: Path) -> Worktree:
    return Worktree(
        id=worktree_id,
        project_id="project-1",
        branch_name=f"task-{worktree_id}",
        worktree_path=str(path),
        base_branch="main",
        status="active",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        task_id=worktree_id,
        agent_session_id=None,
        merged_at=None,
    )


@pytest.mark.asyncio
async def test_four_concurrent_transactions_stay_off_loop_and_finish_cleanup(
    tmp_path: Path,
) -> None:
    worktrees = {
        f"wt-{index}": _worktree(f"wt-{index}", tmp_path / f"wt-{index}") for index in range(4)
    }
    for worktree in worktrees.values():
        Path(worktree.worktree_path).mkdir()
    storage = _Storage(dict(worktrees))
    task_manager = _TaskManager(set(worktrees))
    git_manager = _BarrierGitManager(worker_count=4)
    executor = WorktreeDeleteExecutor(
        max_workers=4,
        thread_name_prefix="test-worktree-delete-barrier",
    )
    main_thread = threading.get_ident()
    heartbeat_ticks = 0
    stop_heartbeat = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not stop_heartbeat.is_set():
            heartbeat_ticks += 1
            await drain_asyncio_tasks()

    def transaction(worktree_id: str) -> Callable[[DestructiveBoundary], Any]:
        request = WorktreeDeletionRequest(worktree_id, DeletionSurface.MCP)
        return lambda boundary: delete_worktree_transaction(
            boundary,
            request=request,
            worktree_storage=cast(LocalWorktreeManager, storage),
            resolve_git_manager=lambda _worktree: cast(WorktreeGitManager, git_manager),
            task_manager=cast(LocalTaskManager, task_manager),
        )

    heartbeat_task = asyncio.create_task(heartbeat())
    delete_tasks = [
        asyncio.create_task(executor.run_delete(transaction(worktree_id)))
        for worktree_id in worktrees
    ]
    try:
        assert await asyncio.to_thread(git_manager.started.wait, 1)
        await wait_for_async_condition(
            lambda: heartbeat_ticks > 1,
            description="event-loop heartbeat while deletes run off-loop",
        )
        stats = executor.stats()
        assert stats.active == 4
        assert stats.max_workers == 4
        assert stats.threads <= 4

        git_manager.release.set()
        results = await asyncio.gather(*delete_tasks)
        assert all(result.success for result in results)
        assert all(result.artifact_refs_cleared == 1 for result in results)
        assert all(result.event is not None for result in results)
        assert storage.rows == {}
        assert task_manager.artifacts.refs == set()
        assert git_manager.max_active == 4
        assert main_thread not in (
            storage.thread_ids | task_manager.artifacts.thread_ids | git_manager.thread_ids
        )
    finally:
        git_manager.release.set()
        stop_heartbeat.set()
        await heartbeat_task
        await asyncio.gather(*delete_tasks, return_exceptions=True)
        executor.shutdown()
        await asyncio.to_thread(executor.join)


@pytest.mark.asyncio
async def test_cancellation_before_mutation_abandons_work() -> None:
    executor = WorktreeDeleteExecutor(max_workers=1)
    precheck_started = threading.Event()
    release_precheck = threading.Event()
    mutated = threading.Event()

    def operation(boundary: DestructiveBoundary) -> str:
        precheck_started.set()
        assert release_precheck.wait(timeout=2)
        if not boundary.begin_mutation():
            return "abandoned"
        mutated.set()
        return "mutated"

    task = asyncio.create_task(executor.run_delete(operation))
    try:
        assert await asyncio.to_thread(precheck_started.wait, 1)
        task.cancel()
        await drain_asyncio_tasks()
        release_precheck.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert mutated.is_set() is False
    finally:
        release_precheck.set()
        executor.shutdown()
        await asyncio.to_thread(executor.join)


@pytest.mark.asyncio
async def test_cancellation_after_mutation_waits_for_cleanup() -> None:
    executor = WorktreeDeleteExecutor(max_workers=1)
    mutation_started = threading.Event()
    release_cleanup = threading.Event()
    cleanup_finished = threading.Event()

    def operation(boundary: DestructiveBoundary) -> None:
        assert boundary.begin_mutation()
        mutation_started.set()
        assert release_cleanup.wait(timeout=2)
        cleanup_finished.set()

    task = asyncio.create_task(executor.run_delete(operation))
    try:
        assert await asyncio.to_thread(mutation_started.wait, 1)
        task.cancel()
        await drain_asyncio_tasks()
        assert task.done() is False
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleanup_finished.is_set()
    finally:
        release_cleanup.set()
        executor.shutdown()
        await asyncio.to_thread(executor.join)


@pytest.mark.asyncio
async def test_shutdown_cancels_queue_drains_active_and_closes_admission() -> None:
    executor = WorktreeDeleteExecutor(max_workers=1)
    active_started = threading.Event()
    release_active = threading.Event()
    queued_started = threading.Event()

    def active(boundary: DestructiveBoundary) -> str:
        assert boundary.begin_mutation()
        active_started.set()
        assert release_active.wait(timeout=2)
        return "active"

    def queued(_boundary: DestructiveBoundary) -> str:
        queued_started.set()
        return "queued"

    active_task = asyncio.create_task(executor.run_delete(active))
    queued_task = asyncio.create_task(executor.run_delete(queued))
    try:
        assert await asyncio.to_thread(active_started.wait, 1)
        await drain_asyncio_tasks()
        assert executor.stats().queued == 1
        executor.shutdown(cancel_futures=True)
        with pytest.raises(RuntimeError, match="shut down"):
            await executor.run_delete(queued)
        release_active.set()
        assert await active_task == "active"
        with pytest.raises(asyncio.CancelledError):
            await queued_task
        await asyncio.to_thread(executor.join)
        stats = executor.stats()
        assert stats.cancelled == 1
        assert stats.active == 0
        assert stats.shutdown is True
        assert queued_started.is_set() is False
    finally:
        release_active.set()
        await asyncio.gather(active_task, queued_task, return_exceptions=True)
        executor.shutdown()
        await asyncio.to_thread(executor.join)
