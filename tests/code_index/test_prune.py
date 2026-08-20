"""Tests for code-index prune automation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.code_index.gcode_gateway import GcodeCommandResult
from gobby.code_index.prune import (
    CODE_INDEX_PRUNE_HANDLER,
    CODE_INDEX_PRUNE_INTERVAL_SECONDS,
    CODE_INDEX_PRUNE_JOB_NAME,
    CODE_INDEX_PRUNE_TIMEOUT_SECONDS,
    CodeIndexPruner,
    register_code_index_prune_cron,
)
from gobby.storage.cron_models import CronJob

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
        self.projects: list[Any] = []
        self.dirty_projects: list[Any] = []
        self.pending_by_project: dict[str, list[Any]] = {}
        self.marked_dirty: list[tuple[str, str, str]] = []
        self.cleared_dirty: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.deleted_hub: list[str] = []

    def list_prune_dirty_projects(
        self,
        limit: int,
        after: tuple[Any, Any, str] | None = None,
    ) -> list[Any]:
        dirty_projects = sorted(
            self.dirty_projects,
            key=lambda dirty: (dirty.updated_at, dirty.created_at, dirty.project_id),
        )
        if after is not None:
            dirty_projects = [
                dirty
                for dirty in dirty_projects
                if (dirty.updated_at, dirty.created_at, dirty.project_id) > after
            ]
        return dirty_projects[:limit]

    def list_indexed_projects(self) -> list[Any]:
        return self.projects

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

    def delete_project_index(self, project_id: str) -> dict[str, int]:
        self.deleted_hub.append(project_id)
        self.projects = [
            project for project in self.projects if getattr(project, "id", "") != project_id
        ]
        return {
            "symbols": 0,
            "files": 0,
            "imports": 0,
            "calls": 0,
            "content_chunks": 0,
            "projects": 1,
        }


class PruneGateway:
    def __init__(
        self,
        *,
        targeted_result: GcodeCommandResult | None = None,
    ) -> None:
        self.targeted_result = targeted_result
        self.retention_days: list[int] = []
        self.targeted_roots: list[Path] = []

    async def prune_project_for_maintenance(
        self,
        project_root: Path,
        *,
        retention_days: int,
        timeout: float | None = None,
        env: dict[str, str] | None = None,
    ) -> GcodeCommandResult:
        del env
        self.retention_days.append(retention_days)
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
        self.launch_factory = None
        self.config = SimpleNamespace(maintenance_log_file=str(log_file), content_retention_days=17)
        self.run_db_calls: list[str] = []

    async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        self.run_db_calls.append(getattr(func, "__name__", repr(func)))
        return func(*args, **kwargs)


def _dirty(
    project_id: str,
    root_path: Path,
    reason: str = "orphan_files",
    *,
    updated_at: str = "2026-01-01T00:00:00+00:00",
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> Any:
    return SimpleNamespace(
        project_id=project_id,
        root_path=str(root_path),
        reason=reason,
        updated_at=updated_at,
        created_at=created_at,
    )


class PersistingPruneStorage(PruneStorage):
    def mark_prune_dirty(self, project_id: str, root_path: str, reason: str) -> None:
        super().mark_prune_dirty(project_id, root_path, reason)
        for dirty in self.dirty_projects:
            if dirty.project_id == project_id:
                dirty.root_path = root_path
                dirty.reason = reason
                return
        self.dirty_projects.append(_dirty(project_id, Path(root_path), reason))

    def clear_prune_dirty(self, project_id: str) -> bool:
        self.dirty_projects = [
            dirty for dirty in self.dirty_projects if dirty.project_id != project_id
        ]
        return super().clear_prune_dirty(project_id)


@pytest.mark.asyncio
async def test_global_prune_runs_in_process_and_clears_dirty_projects(tmp_path: Path) -> None:
    live_root = tmp_path / "one"
    live_root.mkdir()
    storage = PersistingPruneStorage()
    storage.projects = [SimpleNamespace(id="proj-1", root_path=str(live_root))]
    storage.dirty_projects = [_dirty("proj-1", live_root)]
    gateway = PruneGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_all_projects()

    run_id = result["run_id"]
    assert isinstance(run_id, str)
    outcome = {
        "completed": ["proj-1"],
        "failed": [],
        "skipped": [],
    }
    assert result == {
        "success": True,
        "status": "completed",
        "run_id": run_id,
        "message": (
            f"Code index prune completed: run_id={run_id} global:completed failed=0 skipped=0"
        ),
        "stdout": json.dumps(outcome, sort_keys=True),
        "stderr": "",
        "retried_projects": 0,
    }
    assert gateway.retention_days == [17]
    assert gateway.targeted_roots == [live_root]
    assert storage.cleared_dirty == ["proj-1"]
    log_text = (tmp_path / "maintenance.log").read_text(encoding="utf-8")
    assert '"event": "global_prune"' in log_text
    assert '"command": ["in-process", "global_prune"]' in log_text
    assert '"exit_status": 0' in log_text


@pytest.mark.asyncio
async def test_global_prune_failure_leaves_project_dirty(tmp_path: Path) -> None:
    live_root = tmp_path / "one"
    live_root.mkdir()
    storage = PersistingPruneStorage()
    storage.projects = [SimpleNamespace(id="proj-1", root_path=str(live_root))]
    gateway = PruneGateway(
        targeted_result=_gcode_result(
            ("/tmp/gcode", "prune", "--force", "--project", str(live_root)),
            returncode=1,
            stderr="projection prune failed",
        )
    )
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_all_projects()

    run_id = result["run_id"]
    assert isinstance(run_id, str)
    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["retried_projects"] == 0
    assert [(dirty.project_id, dirty.reason) for dirty in storage.dirty_projects] == [
        ("proj-1", "operator_prune_failed")
    ]
    assert storage.cleared_dirty == []
    log_text = (tmp_path / "maintenance.log").read_text(encoding="utf-8")
    assert '"event": "global_prune"' in log_text
    assert '"exit_status": 1' in log_text


@pytest.mark.asyncio
async def test_global_prune_force_and_retention_reach_prune_project(tmp_path: Path) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    storage = PruneStorage()
    storage.projects = [SimpleNamespace(id="proj-live", root_path=str(live_root))]
    gateway = PruneGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]
    lock = pruner._project_locks.setdefault("proj-live", asyncio.Lock())
    await lock.acquire()
    try:
        skipped = await pruner.run_operator_global_prune(force=False)
        assert skipped["skipped"] == [{"project_id": "proj-live", "reason": "skipped_locked"}]
        assert gateway.targeted_roots == []
    finally:
        lock.release()

    result = await pruner.prune_all_projects()

    assert result["success"] is True
    assert result["status"] == "completed"
    assert gateway.retention_days == [17]
    assert gateway.targeted_roots == [live_root]


@pytest.mark.asyncio
async def test_global_prune_held_lock_returns_successful_skip(tmp_path: Path) -> None:
    context = PruneContext(PruneStorage(), PruneGateway(), tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]
    await pruner._global_lock.acquire()

    try:
        result = await pruner.prune_all_projects()
    finally:
        pruner._global_lock.release()

    assert result == {
        "success": True,
        "status": "skipped",
        "run_id": None,
        "message": "Code index prune skipped: global_locked",
        "stdout": "",
        "stderr": "",
        "retried_projects": 0,
    }


@pytest.mark.asyncio
async def test_operator_prune_forwards_retention_days(tmp_path: Path) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    storage = PruneStorage()
    storage.projects = [SimpleNamespace(id="proj-live", root_path=str(live_root))]
    gateway = PruneGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    outcome = await pruner.run_operator_global_prune(force=True, retention_days=9)

    assert outcome["completed"] == ["proj-live"]
    assert gateway.retention_days == [9]


@pytest.mark.asyncio
async def test_operator_prune_skips_locked_unless_forced(tmp_path: Path) -> None:
    live_root = tmp_path / "live"
    live_root.mkdir()
    storage = PruneStorage()
    storage.projects = [SimpleNamespace(id="proj-live", root_path=str(live_root))]
    gateway = PruneGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]
    lock = pruner._project_locks.setdefault("proj-live", asyncio.Lock())
    await lock.acquire()
    try:
        skipped = await pruner.run_operator_global_prune(force=False)
        assert skipped["skipped"] == [{"project_id": "proj-live", "reason": "skipped_locked"}]
        assert gateway.targeted_roots == []
    finally:
        lock.release()

    forced = await pruner.run_operator_global_prune(force=True)
    assert forced["completed"] == ["proj-live"]
    assert gateway.targeted_roots == [live_root]


@pytest.mark.asyncio
async def test_operator_deferred_pending_sync_does_not_delete_hub_rows(tmp_path: Path) -> None:
    missing_root = tmp_path / "gone"
    storage = PruneStorage()
    storage.projects = [SimpleNamespace(id="proj-defer", root_path=str(missing_root))]
    storage.pending_by_project["proj-defer"] = [object()]
    context = PruneContext(storage, PruneGateway(), tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(cast(Any, context))

    outcome = await pruner.run_operator_global_prune()

    assert outcome["completed"] == ["proj-defer"]
    assert outcome["failed"] == []
    assert storage.deleted_hub == []
    assert "delete_project_index" not in context.run_db_calls


@pytest.mark.asyncio
async def test_operator_pruned_missing_root_deletes_via_delete_project_index(
    tmp_path: Path,
) -> None:
    missing_root = tmp_path / "gone"
    storage = PruneStorage()
    storage.projects = [SimpleNamespace(id="proj-pruned", root_path=str(missing_root))]
    context = PruneContext(storage, PruneGateway(), tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(cast(Any, context))

    outcome = await pruner.run_operator_global_prune()

    assert outcome["completed"] == ["proj-pruned"]
    assert storage.deleted_hub == ["proj-pruned"]
    assert "delete_project_index" in context.run_db_calls
    assert "delete_stale_project_records" not in context.run_db_calls


@pytest.mark.asyncio
async def test_operator_reconciled_is_completed_not_failed(tmp_path: Path) -> None:
    missing_root = tmp_path / "gone"

    class RegistryPruneStorage(PruneStorage):
        def get_registry_project(self, project_id: str) -> tuple[bool, bool]:
            assert project_id == "proj-reconcile"
            return True, False

    storage = RegistryPruneStorage()
    storage.projects = [SimpleNamespace(id="proj-reconcile", root_path=str(missing_root))]
    context = PruneContext(storage, PruneGateway(), tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(cast(Any, context))

    outcome = await pruner.run_operator_global_prune()

    assert outcome["completed"] == ["proj-reconcile"]
    assert outcome["failed"] == []
    assert storage.deleted_hub == ["proj-reconcile"]
    assert storage.marked_dirty == []


@pytest.mark.asyncio
async def test_operator_prune_bounds_snapshot_concurrency(tmp_path: Path) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    root_one.mkdir()
    root_two.mkdir()

    class CountingContext(PruneContext):
        def __init__(self, storage: PruneStorage, gateway: PruneGateway, log_file: Path) -> None:
            super().__init__(storage, gateway, log_file)
            self.in_flight = 0
            self.max_in_flight = 0
            self.pending_starts = 0
            self.first_entered = asyncio.Event()
            self.release_first = asyncio.Event()

        async def run_db(self, func: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(func, "__name__", "") == "get_pending_sync_files":
                self.pending_starts += 1
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                if self.pending_starts == 1:
                    self.first_entered.set()
                    await self.release_first.wait()
                try:
                    return func(*args, **kwargs)
                finally:
                    self.in_flight -= 1
            return await super().run_db(func, *args, **kwargs)

    storage = PruneStorage()
    storage.projects = [
        SimpleNamespace(id="proj-one", root_path=str(root_one)),
        SimpleNamespace(id="proj-two", root_path=str(root_two)),
    ]
    context = CountingContext(storage, PruneGateway(), tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(cast(Any, context), max_concurrency=1)

    task = asyncio.create_task(pruner.run_operator_global_prune())
    await context.first_entered.wait()
    assert context.in_flight == 1
    context.release_first.set()
    outcome = await task

    assert sorted(outcome["completed"]) == ["proj-one", "proj-two"]
    assert context.max_in_flight == 1


@pytest.mark.asyncio
async def test_dirty_prune_drains_until_storage_is_empty(tmp_path: Path) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    root_one.mkdir()
    root_two.mkdir()

    class PagingPruneStorage(PruneStorage):
        def list_prune_dirty_projects(
            self,
            _limit: int,
            after: tuple[Any, Any, str] | None = None,
        ) -> list[Any]:
            return super().list_prune_dirty_projects(1, after)

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
async def test_dirty_prune_respects_limit(tmp_path: Path) -> None:
    roots = [tmp_path / name for name in ("one", "two", "three")]
    for root in roots:
        root.mkdir()

    storage = PruneStorage()
    storage.dirty_projects = [
        _dirty("proj-1", roots[0]),
        _dirty("proj-2", roots[1]),
        _dirty("proj-3", roots[2]),
    ]
    gateway = PruneGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_dirty_projects(limit=2)

    assert result == "Code index prune completed: proj-1:pruned, proj-2:pruned"
    assert gateway.targeted_roots == roots[:2]


@pytest.mark.asyncio
async def test_dirty_prune_touches_deferred_rows_after_scan(tmp_path: Path) -> None:
    root_one = tmp_path / "one"
    root_two = tmp_path / "two"
    root_one.mkdir()
    root_two.mkdir()

    storage = PruneStorage()
    storage.dirty_projects = [_dirty("proj-1", root_one), _dirty("proj-2", root_two)]
    storage.pending_by_project["proj-1"] = [object()]
    gateway = PruneGateway()
    context = PruneContext(storage, gateway, tmp_path / "maintenance.log")
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    result = await pruner.prune_dirty_projects(limit=2)

    assert result == "Code index prune completed: proj-1:deferred_pending_sync, proj-2:pruned"
    assert gateway.targeted_roots == [root_two]
    assert storage.marked_dirty == [("proj-1", str(root_one), "orphan_files")]


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
    assert "orphan Qdrant collection cleanup" in storage.created["description"]
    assert "limit" not in storage.created["action_config"]


def test_register_code_index_prune_cron_preserves_disabled_job() -> None:
    now = datetime.now(UTC)
    disabled_job = CronJob(
        id="prune-job",
        project_id="personal",
        name=CODE_INDEX_PRUNE_JOB_NAME,
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": CODE_INDEX_PRUNE_HANDLER},
        created_at=now,
        updated_at=now,
        interval_seconds=CODE_INDEX_PRUNE_INTERVAL_SECONDS,
        enabled=False,
        is_system=True,
        next_run_at=None,
    )

    class CronStorage:
        def __init__(self) -> None:
            self.definition_update: dict[str, Any] | None = None

        def get_job_by_name(self, _name: str) -> CronJob:
            return disabled_job

        def reconcile_system_job_definition(self, _job_id: str, **fields: Any) -> CronJob:
            self.definition_update = fields
            return disabled_job

        def reconcile_system_job_identity(self, _job_id: str, **_fields: Any) -> None:
            pytest.fail("disabled jobs must retain their operator-controlled enabled state")

        def wake_system_job(self, _job_id: str) -> None:
            pytest.fail("disabled jobs must not be woken")

    class CronExecutor:
        def register_handler(self, _name: str, _handler: Any) -> None:
            pass

    storage = CronStorage()
    context = PruneContext(PruneStorage(), PruneGateway(), Path("/tmp/maintenance.log"))
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    register_code_index_prune_cron(
        cron_storage=storage,  # type: ignore[arg-type]
        cron_executor=CronExecutor(),
        pruner=pruner,
        project_id="personal",
    )

    assert disabled_job.enabled is False
    assert storage.definition_update is not None
    assert "orphan Qdrant collection cleanup" in storage.definition_update["description"]


def test_register_code_index_prune_cron_wakes_enabled_job_without_next_run() -> None:
    now = datetime.now(UTC)
    enabled_job = CronJob(
        id="prune-job",
        project_id="personal",
        name=CODE_INDEX_PRUNE_JOB_NAME,
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": CODE_INDEX_PRUNE_HANDLER},
        created_at=now,
        updated_at=now,
        interval_seconds=CODE_INDEX_PRUNE_INTERVAL_SECONDS,
        enabled=True,
        is_system=True,
        next_run_at=None,
    )

    class CronStorage:
        def __init__(self) -> None:
            self.woken: list[str] = []

        def get_job_by_name(self, _name: str) -> CronJob:
            return enabled_job

        def reconcile_system_job_definition(self, _job_id: str, **_fields: Any) -> CronJob:
            return enabled_job

        def reconcile_system_job_identity(self, _job_id: str, **_fields: Any) -> None:
            pytest.fail("enabled identity must not be rewritten")

        def wake_system_job(self, job_id: str) -> None:
            self.woken.append(job_id)

    class CronExecutor:
        def register_handler(self, _name: str, _handler: Any) -> None:
            pass

    storage = CronStorage()
    context = PruneContext(PruneStorage(), PruneGateway(), Path("/tmp/maintenance.log"))
    pruner = CodeIndexPruner(context)  # type: ignore[arg-type]

    register_code_index_prune_cron(
        cron_storage=storage,  # type: ignore[arg-type]
        cron_executor=CronExecutor(),
        pruner=pruner,
        project_id="personal",
    )

    assert storage.woken == ["prune-job"]
