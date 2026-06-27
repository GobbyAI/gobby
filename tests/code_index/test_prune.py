"""Tests for code-index prune automation."""

from __future__ import annotations

import json
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.code_index.gcode_gateway import GcodeCommandResult
from gobby.code_index.models import IndexedProject
from gobby.code_index.prune import (
    CODE_INDEX_PRUNE_HANDLER,
    CODE_INDEX_PRUNE_INTERVAL_SECONDS,
    CODE_INDEX_PRUNE_JOB_NAME,
    CODE_INDEX_PRUNE_TIMEOUT_SECONDS,
    CodeIndexPruner,
    register_code_index_prune_cron,
)

pytestmark = pytest.mark.unit


def _gcode_result(
    command: tuple[str, ...],
    *,
    returncode: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> GcodeCommandResult:
    return GcodeCommandResult(
        command=command,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        duration_seconds=1.0,
        timeout_seconds=CODE_INDEX_PRUNE_TIMEOUT_SECONDS,
        timed_out=timed_out,
    )


class PruneStorage:
    def __init__(self) -> None:
        self.projects: list[IndexedProject] = []
        self.dirty_projects: list[Any] = []
        self.pending_by_project: dict[str, list[Any]] = {}
        self.marked_dirty: list[tuple[str, str, str]] = []
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

    def mark_prune_dirty(self, project_id: str, root_path: str, reason: str) -> None:
        self.marked_dirty.append((project_id, root_path, reason))

    def clear_prune_dirty(self, project_id: str) -> bool:
        self.cleared_dirty.append(project_id)
        return True

    def record_prune_failure(self, project_id: str, error: str) -> None:
        self.failures.append((project_id, error))


class PruneGateway:
    def __init__(
        self,
        *,
        global_result: GcodeCommandResult | None = None,
        targeted_result: GcodeCommandResult | None = None,
    ) -> None:
        self.global_result = global_result or _gcode_result(("/tmp/gcode", "prune", "--force"))
        self.targeted_result = targeted_result
        self.global_timeouts: list[float | None] = []
        self.targeted_roots: list[Path] = []

    async def prune_all_projects(self, *, timeout: float | None = None) -> GcodeCommandResult:
        self.global_timeouts.append(timeout)
        return self.global_result

    async def prune_project_for_maintenance(
        self,
        project_root: Path,
        *,
        timeout: float | None = None,
    ) -> GcodeCommandResult:
        self.targeted_roots.append(project_root)
        return self.targeted_result or _gcode_result(
            ("/tmp/gcode", "prune", "--force", "--project", str(project_root))
        )


class PruneContext:
    def __init__(
        self,
        storage: PruneStorage,
        gateway: PruneGateway | None,
        log_file: Path,
    ) -> None:
        self.storage = storage
        self.gcode_gateway = gateway
        self.config = SimpleNamespace(maintenance_log_file=str(log_file))
        self.run_db_calls: list[str] = []

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        self.run_db_calls.append(getattr(func, "__name__", repr(func)))
        return func(*args, **kwargs)


def _project(project_id: str, root_path: Path | None) -> IndexedProject:
    return IndexedProject(
        id=project_id,
        root_path=str(root_path) if root_path is not None else None,
        total_files=1,
        total_symbols=1,
    )


def _dirty(project_id: str, root_path: Path, reason: str = "orphan_files") -> Any:
    return SimpleNamespace(project_id=project_id, root_path=str(root_path), reason=reason)


@pytest.mark.asyncio
async def test_global_prune_runs_once_and_clears_dirty_projects(tmp_path: Path) -> None:
    storage = PruneStorage()
    storage.projects = [_project("proj-1", tmp_path / "one")]
    storage.dirty_projects = [_dirty("proj-1", tmp_path / "one")]
    gateway = PruneGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_all_projects()

    assert result.startswith("Code index prune completed: run_id=")
    assert result.endswith("global:pruned")
    assert gateway.global_timeouts == [CODE_INDEX_PRUNE_TIMEOUT_SECONDS]
    assert gateway.targeted_roots == []
    assert storage.cleared_dirty == ["proj-1"]
    log_text = (tmp_path / "maintenance.log").read_text(encoding="utf-8")
    assert '"event": "global_prune"' in log_text
    assert '"command": ["/tmp/gcode", "prune", "--force"]' in log_text


@pytest.mark.asyncio
async def test_global_prune_failure_retries_all_indexed_project_roots(
    tmp_path: Path,
) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    storage = PruneStorage()
    storage.projects = [
        _project("proj-1", root_one),
        _project("proj-2", root_two),
        _project("proj-missing", None),
    ]
    gateway = PruneGateway(
        global_result=_gcode_result(
            ("/tmp/gcode", "prune", "--force"),
            returncode=1,
            stderr="projection prune failed",
        )
    )
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_all_projects()

    assert "global:failed retries=2" in result
    assert storage.marked_dirty == [
        ("proj-1", str(root_one), "global_prune_failed"),
        ("proj-2", str(root_two), "global_prune_failed"),
    ]
    assert storage.failures == [
        ("proj-1", "projection prune failed"),
        ("proj-2", "projection prune failed"),
    ]
    assert gateway.targeted_roots == [root_one, root_two]
    assert storage.cleared_dirty == ["proj-1", "proj-2"]


@pytest.mark.asyncio
async def test_global_prune_failure_retries_structured_failed_project_ids(
    tmp_path: Path,
) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    storage = PruneStorage()
    storage.projects = [_project("proj-1", root_one), _project("proj-2", root_two)]
    gateway = PruneGateway(
        global_result=_gcode_result(
            ("/tmp/gcode", "prune", "--force"),
            returncode=1,
            stderr=json.dumps({"failed_project_ids": ["proj-2"]}),
        )
    )
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_all_projects()

    assert "global:failed retries=1" in result
    assert storage.marked_dirty == [("proj-2", str(root_two), "global_prune_failed")]
    assert gateway.targeted_roots == [root_two]


@pytest.mark.asyncio
async def test_global_prune_sigterm_with_no_stale_stdout_is_completed(
    tmp_path: Path,
) -> None:
    storage = PruneStorage()
    gateway = PruneGateway(
        global_result=_gcode_result(
            ("/tmp/gcode", "prune", "--force"),
            returncode=-signal.SIGTERM,
            stdout="No stale projects found.",
        )
    )
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_all_projects()

    assert result.endswith("global:pruned")


@pytest.mark.asyncio
async def test_dirty_prune_drains_until_storage_is_empty(tmp_path: Path) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    root_one.mkdir()
    root_two.mkdir()

    class PagingPruneStorage(PruneStorage):
        def list_prune_dirty_projects(self, _limit: int) -> list[Any]:
            return self.dirty_projects[:1]

        def clear_prune_dirty(self, project_id: str) -> bool:
            self.dirty_projects = [
                dirty for dirty in self.dirty_projects if dirty.project_id != project_id
            ]
            return super().clear_prune_dirty(project_id)

    storage = PagingPruneStorage()
    storage.dirty_projects = [_dirty("proj-1", root_one), _dirty("proj-2", root_two)]
    gateway = PruneGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_dirty_projects()

    assert result == "Code index prune completed: proj-1:pruned, proj-2:pruned"
    assert gateway.targeted_roots == [root_one, root_two]
    assert storage.dirty_projects == []


@pytest.mark.asyncio
async def test_targeted_retry_defers_while_sync_file_work_is_pending(tmp_path: Path) -> None:
    storage = PruneStorage()
    storage.pending_by_project["proj-1"] = [object()]
    gateway = PruneGateway()
    pruner = CodeIndexPruner(PruneContext(storage, gateway, tmp_path / "maintenance.log"))  # type: ignore[arg-type]

    result = await pruner.prune_project(
        project_id="proj-1",
        root_path=str(tmp_path),
        dirty=True,
        reason="global_prune_retry",
    )

    assert result == "proj-1:deferred_pending_sync"
    assert gateway.targeted_roots == []
    assert storage.cleared_dirty == []
    assert storage.failures == []


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
    context = PruneContext(PruneStorage(), PruneGateway(), Path("/tmp/maintenance.log"))
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

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
    assert "limit" not in storage.created["action_config"]
