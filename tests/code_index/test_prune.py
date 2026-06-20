"""Tests for code-index prune automation."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.code_index.gcode_gateway import GcodeCommandError
from gobby.code_index.models import IndexedProject
from gobby.code_index.prune import (
    CODE_INDEX_PRUNE_HANDLER,
    CODE_INDEX_PRUNE_INTERVAL_SECONDS,
    CODE_INDEX_PRUNE_JOB_NAME,
    CodeIndexPruner,
    register_code_index_prune_cron,
)

pytestmark = pytest.mark.unit


class PruneStorage:
    def __init__(self) -> None:
        self.projects: list[IndexedProject] = []
        self.dirty_projects: list[Any] = []
        self.pending_by_project: dict[str, list[Any]] = {}
        self.cleared_dirty: list[str] = []
        self.failures: list[tuple[str, str]] = []

    def list_indexed_projects(self) -> list[IndexedProject]:
        return self.projects

    def list_prune_dirty_projects(self, _limit: int) -> list[Any]:
        return self.dirty_projects

    def get_pending_sync_files(
        self,
        project_id: str,
        _limit: int,
        *,
        vectors: bool,
        graph: bool,
    ) -> list[Any]:
        assert vectors is True
        assert graph is True
        return self.pending_by_project.get(project_id, [])

    def clear_prune_dirty(self, project_id: str) -> bool:
        self.cleared_dirty.append(project_id)
        return True

    def record_prune_failure(self, project_id: str, error: str) -> None:
        self.failures.append((project_id, error))


class PruneGateway:
    def __init__(self, *, result: dict[str, Any] | None = None, delay: float = 0) -> None:
        self.result = result or {"success": True}
        self.delay = delay
        self.pruned_roots: list[Path] = []
        self.active = 0
        self.max_active = 0

    async def prune(self, project_root: Path) -> dict[str, Any]:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.pruned_roots.append(project_root)
            return self.result
        finally:
            self.active -= 1


class PruneContext:
    def __init__(self, storage: PruneStorage, gateway: PruneGateway | None) -> None:
        self.storage = storage
        self.gcode_gateway = gateway
        self.run_db_calls: list[str] = []

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        self.run_db_calls.append(getattr(func, "__name__", repr(func)))
        return func(*args, **kwargs)


def _dirty(project_id: str, root_path: Path, reason: str = "orphan_files") -> Any:
    return SimpleNamespace(project_id=project_id, root_path=str(root_path), reason=reason)


@pytest.mark.asyncio
async def test_prune_dirty_projects_skips_when_no_dirty_projects() -> None:
    storage = PruneStorage()
    context = PruneContext(storage, PruneGateway())
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_dirty_projects()

    assert result == "Code index prune skipped: dirty=0"
    assert context.run_db_calls == ["list_prune_dirty_projects"]


@pytest.mark.asyncio
async def test_prune_dirty_projects_runs_and_clears_dirty_project(tmp_path: Path) -> None:
    storage = PruneStorage()
    storage.dirty_projects = [_dirty("proj-1", tmp_path, "invalidate")]
    gateway = PruneGateway()
    pruner = CodeIndexPruner(PruneContext(storage, gateway))  # type: ignore[arg-type]

    result = await pruner.prune_dirty_projects(limit=5)

    assert result == "Code index prune completed: proj-1:pruned"
    assert gateway.pruned_roots == [tmp_path]
    assert storage.cleared_dirty == ["proj-1"]
    assert storage.failures == []


@pytest.mark.asyncio
async def test_prune_project_defers_while_sync_file_work_is_pending(tmp_path: Path) -> None:
    storage = PruneStorage()
    storage.pending_by_project["proj-1"] = [object()]
    gateway = PruneGateway()
    pruner = CodeIndexPruner(PruneContext(storage, gateway))  # type: ignore[arg-type]

    result = await pruner.prune_project(
        project_id="proj-1",
        root_path=str(tmp_path),
        dirty=True,
        reason="orphan_files",
    )

    assert result == "proj-1:deferred_pending_sync"
    assert gateway.pruned_roots == []
    assert storage.cleared_dirty == []
    assert storage.failures == []


@pytest.mark.asyncio
async def test_prune_project_records_dirty_failure_when_gateway_missing(tmp_path: Path) -> None:
    storage = PruneStorage()
    pruner = CodeIndexPruner(PruneContext(storage, None))  # type: ignore[arg-type]

    result = await pruner.prune_project(
        project_id="proj-1",
        root_path=str(tmp_path),
        dirty=True,
        reason="orphan_files",
    )

    assert result == "proj-1:failed"
    assert storage.failures == [("proj-1", "gcode gateway unavailable")]


@pytest.mark.asyncio
async def test_prune_project_treats_sigterm_no_stale_result_as_success(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class SigtermNoopGateway(PruneGateway):
        async def prune(self, project_root: Path) -> dict[str, Any]:
            self.pruned_roots.append(project_root)
            raise GcodeCommandError(
                ["gcode", "prune", "--project", str(project_root)],
                -signal.SIGTERM,
                "No stale projects found.",
            )

    storage = PruneStorage()
    gateway = SigtermNoopGateway()
    pruner = CodeIndexPruner(PruneContext(storage, gateway))  # type: ignore[arg-type]

    caplog.set_level(logging.WARNING, logger="gobby.code_index.prune")

    result = await pruner.prune_project(
        project_id="proj-1",
        root_path=str(tmp_path),
        dirty=True,
        reason="shutdown",
    )

    assert result == "proj-1:pruned"
    assert gateway.pruned_roots == [tmp_path]
    assert storage.cleared_dirty == ["proj-1"]
    assert storage.failures == []
    assert "Code index prune failed" not in caplog.text


@pytest.mark.asyncio
async def test_prune_project_skips_when_project_lock_is_held(tmp_path: Path) -> None:
    storage = PruneStorage()
    pruner = CodeIndexPruner(PruneContext(storage, PruneGateway()))  # type: ignore[arg-type]
    lock = pruner._project_locks.setdefault("proj-1", asyncio.Lock())
    await lock.acquire()
    try:
        result = await pruner.prune_project(
            project_id="proj-1",
            root_path=str(tmp_path),
            dirty=True,
            reason="orphan_files",
        )
    finally:
        lock.release()

    assert result == "proj-1:skipped_locked"
    assert storage.cleared_dirty == []


@pytest.mark.asyncio
async def test_prune_project_global_concurrency_cap_is_one(tmp_path: Path) -> None:
    storage = PruneStorage()
    gateway = PruneGateway(delay=0.01)
    pruner = CodeIndexPruner(PruneContext(storage, gateway), max_concurrency=1)  # type: ignore[arg-type]

    results = await asyncio.gather(
        pruner.prune_project(
            project_id="proj-1",
            root_path=str(tmp_path / "one"),
            dirty=False,
            reason="startup",
        ),
        pruner.prune_project(
            project_id="proj-2",
            root_path=str(tmp_path / "two"),
            dirty=False,
            reason="startup",
        ),
    )

    assert results == ["proj-1:pruned", "proj-2:pruned"]
    assert gateway.max_active == 1


@pytest.mark.asyncio
async def test_startup_prune_schedules_one_background_prune_per_project(
    tmp_path: Path,
) -> None:
    storage = PruneStorage()
    storage.projects = [
        IndexedProject(
            id="proj-1", root_path=str(tmp_path / "one"), total_files=1, total_symbols=1
        ),
        IndexedProject(id="proj-2", root_path=None, total_files=0, total_symbols=0),
    ]
    gateway = PruneGateway()
    pruner = CodeIndexPruner(PruneContext(storage, gateway))  # type: ignore[arg-type]

    result = await pruner.schedule_startup_prunes()
    tasks = list(pruner._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)

    assert result == {"scheduled": 1, "skipped": 1}
    assert gateway.pruned_roots == [tmp_path / "one"]


def test_register_code_index_prune_cron_creates_hourly_system_job() -> None:
    class CronStorage:
        def __init__(self) -> None:
            self.created: dict[str, Any] | None = None

        def get_job_by_name(self, name: str) -> None:
            assert name == CODE_INDEX_PRUNE_JOB_NAME
            return None

        def create_job(self, **kwargs: Any) -> None:
            self.created = kwargs

    class CronExecutor:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def register_handler(self, name: str, handler: Any) -> None:
            self.handlers[name] = handler

    storage = CronStorage()
    executor = CronExecutor()
    pruner = CodeIndexPruner(PruneContext(PruneStorage(), PruneGateway()))  # type: ignore[arg-type]

    register_code_index_prune_cron(
        cron_storage=storage,  # type: ignore[arg-type]
        cron_executor=executor,
        pruner=pruner,
        project_id="personal",
    )

    assert CODE_INDEX_PRUNE_HANDLER in executor.handlers
    assert storage.created is not None
    assert storage.created["name"] == CODE_INDEX_PRUNE_JOB_NAME
    assert storage.created["interval_seconds"] == CODE_INDEX_PRUNE_INTERVAL_SECONDS
    assert storage.created["is_system"] is True
    assert storage.created["action_config"]["handler"] == CODE_INDEX_PRUNE_HANDLER
