from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from gobby.projects.purge import (
    PROJECT_PURGE_CONCURRENCY,
    PROJECT_PURGE_DESCRIPTION,
    PROJECT_PURGE_HANDLER_NAME,
    PROJECT_PURGE_INTERVAL_SECONDS,
    PROJECT_PURGE_JOB_NAME,
    ProjectPurgeService,
    ProjectPurgeVectorStoreUnavailable,
    PurgeOutcome,
    create_project_purge_handler,
    register_project_purge_cron,
)
from gobby.runtime_grants.launch import ManagedLaunch
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import PERSONAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase


@dataclass
class FakeProject:
    id: str
    name: str
    deleted_at: datetime | None = None


class FakeProjects:
    def __init__(self, project: FakeProject) -> None:
        self.project = project
        self.hard_deleted = False

    def get(self, project_id: str) -> FakeProject | None:
        return None if self.hard_deleted or project_id != self.project.id else self.project

    def is_protected(self, project: FakeProject) -> bool:
        return project.name == "gobby"

    def soft_delete(self, project_id: str) -> bool:
        assert project_id == self.project.id
        self.project.deleted_at = datetime.now(UTC)
        return True

    def list_purge_candidates(self, _cutoff: datetime) -> list[FakeProject]:
        return [self.project]


class FakeTransaction:
    def __init__(self, db: FakeDB) -> None:
        self.db = db

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> SimpleNamespace:
        stripped = sql.strip()
        if "pg_advisory_xact_lock" in stripped:
            return SimpleNamespace(rowcount=1)
        if stripped.startswith("INSERT INTO embedding_projection_changes"):
            self.db.tombstones.append((str(params[0]), str(params[1]), bool(params[2])))
            self.db.sequence += 1
            sequence = self.db.sequence
            return SimpleNamespace(fetchone=lambda: {"sequence": sequence})
        _head, separator, tail = sql.partition("FROM ")
        if not separator:
            statement = stripped.split(maxsplit=1)[0]
            raise AssertionError(f"Unsupported SQL statement in fake transaction: {statement}")
        table = tail.split()[0]
        if stripped.upper().startswith("SELECT"):
            rows: list[dict[str, Any]] = []
            if table == "memories" and not self.db.memories_purged:
                # Purge batches until a SELECT returns nothing; the fake drains
                # after the DELETE so the loop terminates like the real table.
                rows = [
                    {"row_id": "memory-1", "source_id": "memory-1"},
                    {"row_id": "memory-2", "source_id": "memory-2"},
                ]
            return SimpleNamespace(fetchall=lambda: rows)
        self.db.events.append(f"sql:{table}")
        if table == "memories":
            self.db.memories_purged = True
        if table == "projects":
            self.db.projects.hard_deleted = True
        return SimpleNamespace(rowcount=1)


class FakeDB:
    def __init__(self, events: list[str], projects: FakeProjects) -> None:
        self.events = events
        self.projects = projects
        self.tombstones: list[tuple[str, str, bool]] = []
        self.sequence = 0
        self.memories_purged = False

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, str]]:
        del params
        if "FROM memories" in sql:
            return [{"id": "memory-1"}, {"id": "memory-2"}]
        return []

    @contextmanager
    def transaction(self) -> Iterator[FakeTransaction]:
        self.events.append("hub:begin")
        yield FakeTransaction(self)
        self.events.append("hub:commit")


class FakeCron:
    def __init__(self, events: list[str], *, active: bool = False) -> None:
        self.events = events
        self.active = active

    def disable_project_jobs(self, project_id: str) -> list[SimpleNamespace]:
        self.events.append("cron:disable")
        return [SimpleNamespace(id="job-1", project_id=project_id, name="project:refresh")]

    def list_active_runs(self) -> list[SimpleNamespace]:
        self.events.append("cron:drain")
        if self.active:
            return [SimpleNamespace(cron_job_id="job-1")]
        return []

    def delete_project_jobs(self, job_ids: list[str]) -> int:
        assert job_ids == ["job-1"]
        self.events.append("cron:delete")
        return 1


class FakeFence:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def exclusive(self, project_id: str, *, timeout: float) -> AsyncIterator[None]:
        del project_id, timeout
        self.events.append("fence:enter")
        try:
            yield
        finally:
            self.events.append("fence:exit")


class FakeCodeGateway:
    def __init__(self, events: list[str], *, success: bool = True) -> None:
        self.events = events
        self.success = success
        self.envs: list[Mapping[str, str] | None] = []

    async def invalidate_project_by_id(
        self, project_id: str, *, timeout: float, env: Mapping[str, str] | None = None
    ) -> SimpleNamespace:
        del project_id, timeout
        self.events.append("code:invalidate")
        self.envs.append(env)
        return SimpleNamespace(success=self.success, stderr="code failed")


class FakeLaunchFactory:
    """Maintenance launch factory that records every grant it opens."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.opened: list[str] = []

    @contextmanager
    def open(
        self,
        project_id: str,
        *,
        timeout_seconds: float,
        code_overlay_project_id: str | None = None,
    ) -> Iterator[ManagedLaunch]:
        del timeout_seconds, code_overlay_project_id
        self.opened.append(project_id)
        self.events.append("launch:open")
        try:
            yield ManagedLaunch(
                grant_path=Path("/nonexistent/grant.json"),
                env={"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": f"/grants/{project_id}.json"},
            )
        finally:
            self.events.append("launch:close")


class FakeVectorCleaner:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.memory_ids: list[str] = []

    async def clear_project(self, project_id: str, memory_ids: list[str]) -> None:
        del project_id
        self.memory_ids = memory_ids
        self.events.append("vectors:clear")


class FakeGraphCleaner:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def clear_project_graph_strict(self, project_id: str) -> dict[str, int]:
        del project_id
        self.events.append("graph:clear")
        return {"memories_deleted": 1, "entities_deleted": 1}


def make_service(
    project: FakeProject,
    *,
    code_success: bool = True,
    active_cron: bool = False,
    launch_factory: FakeLaunchFactory | None = None,
) -> tuple[ProjectPurgeService, FakeProjects, FakeVectorCleaner, list[str]]:
    events: list[str] = []
    projects = FakeProjects(project)
    vectors = FakeVectorCleaner(events)
    service = ProjectPurgeService(
        launch_factory=(lambda: launch_factory) if launch_factory is not None else None,
        db=FakeDB(events, projects),
        projects=projects,
        cron=FakeCron(events, active=active_cron),
        fence=FakeFence(events),
        code_gateway=FakeCodeGateway(events, success=code_success),
        vector_cleaner=lambda: vectors,
        graph_cleaner=lambda: FakeGraphCleaner(events),
        drain_timeout=0.01,
    )
    return service, projects, vectors, events


@pytest.mark.asyncio
async def test_protection_gate_runs_before_soft_delete_or_cleanup() -> None:
    service, projects, _vectors, events = make_service(FakeProject("p1", "gobby"))

    result = await service.purge_project("p1")

    assert not result.success
    assert result.status == "protected"
    assert projects.project.deleted_at is None
    assert events == []


@pytest.mark.asyncio
async def test_purge_orders_quiescence_projections_cleanup_and_hub_transaction() -> None:
    service, projects, vectors, events = make_service(FakeProject("p1", "app"))

    result = await service.purge_project("p1")

    assert result.success
    assert projects.get("p1") is None
    assert vectors.memory_ids == ["memory-1", "memory-2"]
    fake_db = cast(FakeDB, service.db)
    assert fake_db.tombstones == [
        ("memory", "memory-1", True),
        ("memory", "memory-2", True),
    ]
    assert events == [
        "cron:disable",
        "cron:drain",
        "cron:delete",
        "fence:enter",
        "code:invalidate",
        "vectors:clear",
        "graph:clear",
        # Tombstone batches run one hub transaction per batch: the memory batch
        # deletes and tombstones, then the memory and tool kinds drain with an
        # empty SELECT.
        "hub:begin",
        "sql:memories",
        "hub:commit",
        "hub:begin",
        "hub:commit",
        "hub:begin",
        "hub:commit",
        "hub:begin",
        # Cross-project references are detached (or dropped) before the deletes;
        # the fake names each statement by its first FROM table.
        "sql:tasks",
        "sql:sessions",
        "sql:sessions",
        "sql:sessions",
        "sql:sessions",
        "sql:workflow_audit_log",
        "sql:agent_runs",
        "sql:sessions",
        "sql:sessions",
        "sql:tasks",
        "sql:plans",
        "sql:sessions",
        "sql:projects",
        "hub:commit",
        "fence:exit",
    ]


@pytest.mark.asyncio
async def test_unavailable_vector_store_fails_purge_before_destructive_cleanup() -> None:
    service, projects, _vectors, events = make_service(FakeProject("p1", "app"))

    def unavailable_cleaner() -> FakeVectorCleaner:
        raise ProjectPurgeVectorStoreUnavailable(
            "Qdrant is configured but the runtime vector store is unavailable"
        )

    service.vector_cleaner = unavailable_cleaner

    result = await service.purge_project("p1")

    assert not result.success
    assert projects.get("p1") is not None
    assert projects.project.deleted_at is not None
    assert "cron:disable" not in events
    assert "cron:delete" not in events
    assert "fence:enter" not in events
    assert "hub:begin" not in events


@pytest.mark.asyncio
async def test_failed_projection_or_busy_cron_keeps_soft_deleted_retry_anchor() -> None:
    service, projects, _vectors, events = make_service(FakeProject("p1", "app"), code_success=False)

    result = await service.purge_project("p1")

    assert not result.success
    assert projects.get("p1") is not None
    assert projects.project.deleted_at is not None
    assert "hub:begin" not in events

    busy_service, busy_projects, _vectors, busy_events = make_service(
        FakeProject("p2", "other"), active_cron=True
    )
    busy_result = await busy_service.purge_project("p2")
    assert not busy_result.success
    assert busy_projects.project.deleted_at is not None
    assert "cron:delete" not in busy_events


@pytest.mark.asyncio
async def test_daily_handler_isolates_failures_and_bounds_id_lists() -> None:
    class BatchService:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.projects = SimpleNamespace(
                list_purge_candidates=lambda _cutoff: [
                    FakeProject(f"p{i}", f"project-{i}", datetime.now(UTC) - timedelta(days=31))
                    for i in range(15)
                ]
            )
            self.active = 0
            self.max_active = 0
            self.batch_full = asyncio.Event()

        async def purge_project(self, project_id: str) -> PurgeOutcome:
            self.calls.append(project_id)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == PROJECT_PURGE_CONCURRENCY:
                self.batch_full.set()
            try:
                await self.batch_full.wait()
                if project_id == "p5":
                    raise RuntimeError("unexpected service failure")
                if project_id in {"p1", "p11"}:
                    return PurgeOutcome.failed(project_id, "derived cleanup failed")
                return PurgeOutcome.purged(project_id)
            finally:
                self.active -= 1

    service = BatchService()
    handler = create_project_purge_handler(service)
    result = await handler(SimpleNamespace())

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["purged_count"] == 12
    assert result["failed_count"] == 3
    assert len(result["purged"]) == 10
    assert result["failed"] == ["p1", "p5", "p11"]
    assert set(service.calls) == {f"p{i}" for i in range(15)}
    assert service.max_active == PROJECT_PURGE_CONCURRENCY


def _create_purge_cron_job(
    storage: CronJobStorage,
    *,
    enabled: bool = True,
    drifted: bool = False,
) -> CronJob:
    return storage.create_job(
        project_id=PERSONAL_PROJECT_ID,
        name=PROJECT_PURGE_JOB_NAME,
        description="stale purge definition" if drifted else PROJECT_PURGE_DESCRIPTION,
        schedule_type="interval",
        interval_seconds=(
            PROJECT_PURGE_INTERVAL_SECONDS * 2 if drifted else PROJECT_PURGE_INTERVAL_SECONDS
        ),
        action_type="handler",
        action_config=(
            {"handler": PROJECT_PURGE_HANDLER_NAME}
            if drifted
            else {
                "handler": PROJECT_PURGE_HANDLER_NAME,
                "purpose": PROJECT_PURGE_DESCRIPTION,
            }
        ),
        enabled=enabled,
        is_system=True,
    )


def _register_purge_cron(storage: CronJobStorage) -> dict[str, Any]:
    handlers: dict[str, Any] = {}
    executor = SimpleNamespace(
        register_handler=lambda name, handler: handlers.__setitem__(name, handler)
    )
    register_project_purge_cron(storage, executor, cast(Any, SimpleNamespace()))
    return handlers


def test_purge_cron_registration_preserves_parked_job_without_drift(
    temp_db: HubDatabase,
) -> None:
    storage = CronJobStorage(temp_db)
    parked = _create_purge_cron_job(storage)
    storage.park_system_job(parked.id)

    _register_purge_cron(storage)

    reconciled = storage.get_job(parked.id)
    assert reconciled is not None
    assert reconciled.enabled
    assert reconciled.next_run_at is None


def test_purge_cron_registration_preserves_parked_job_with_drift(
    temp_db: HubDatabase,
) -> None:
    storage = CronJobStorage(temp_db)
    parked = _create_purge_cron_job(storage, drifted=True)
    storage.park_system_job(parked.id)

    _register_purge_cron(storage)

    reconciled = storage.get_job(parked.id)
    assert reconciled is not None
    assert reconciled.description == PROJECT_PURGE_DESCRIPTION
    assert reconciled.interval_seconds == PROJECT_PURGE_INTERVAL_SECONDS
    assert reconciled.action_config["purpose"] == PROJECT_PURGE_DESCRIPTION
    assert reconciled.next_run_at is None


def test_purge_cron_registration_recomputes_scheduled_job_with_drift(
    temp_db: HubDatabase,
) -> None:
    storage = CronJobStorage(temp_db)
    scheduled = _create_purge_cron_job(storage, drifted=True)
    previous_next_run = scheduled.next_run_at

    _register_purge_cron(storage)

    reconciled = storage.get_job(scheduled.id)
    assert reconciled is not None
    assert reconciled.interval_seconds == PROJECT_PURGE_INTERVAL_SECONDS
    assert reconciled.next_run_at is not None
    assert reconciled.next_run_at != previous_next_run


def test_purge_cron_registration_schedules_created_job(temp_db: HubDatabase) -> None:
    storage = CronJobStorage(temp_db)

    handlers = _register_purge_cron(storage)

    created = storage.get_job_by_name(PROJECT_PURGE_JOB_NAME)
    assert created is not None
    assert created.enabled
    assert created.is_system
    assert created.next_run_at is not None
    assert PROJECT_PURGE_HANDLER_NAME in handlers


def test_purge_cron_registration_preserves_disabled_job(temp_db: HubDatabase) -> None:
    storage = CronJobStorage(temp_db)
    disabled = _create_purge_cron_job(storage, enabled=False, drifted=True)

    _register_purge_cron(storage)

    reconciled = storage.get_job(disabled.id)
    assert reconciled is not None
    assert not reconciled.enabled
    assert reconciled.next_run_at is None
    assert reconciled.description == PROJECT_PURGE_DESCRIPTION


@pytest.mark.asyncio
async def test_purge_runs_projection_cleanup_under_a_maintenance_launch() -> None:
    """A soft-deleted project has no checkout and is refused an interactive grant.

    gcode invalidate therefore runs inside a maintenance launch,
    whose grant bootstrap env is handed to each child and released afterwards.
    """
    events: list[str] = []
    factory = FakeLaunchFactory(events)
    service, projects, _vectors, events = make_service(
        FakeProject("p1", "app"), launch_factory=factory
    )
    factory.events = events

    result = await service.purge_project("p1")

    assert result.success
    assert projects.get("p1") is None
    assert factory.opened == ["p1"]
    code = cast(FakeCodeGateway, service.code_gateway)
    assert code.envs == [{"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/grants/p1.json"}]
    purge_window = events[events.index("fence:enter") + 1 : events.index("vectors:clear")]
    assert purge_window == [
        "launch:open",
        "code:invalidate",
        "launch:close",
    ]


@pytest.mark.asyncio
async def test_purge_without_a_launch_factory_passes_no_grant_env() -> None:
    service, _projects, _vectors, _events = make_service(FakeProject("p1", "app"))

    result = await service.purge_project("p1")

    assert result.success
    assert cast(FakeCodeGateway, service.code_gateway).envs == [None]
