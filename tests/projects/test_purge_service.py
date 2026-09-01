from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.projects.purge import (
    PROJECT_PURGE_CONCURRENCY,
    PROJECT_PURGE_HANDLER_NAME,
    PROJECT_PURGE_JOB_NAME,
    ProjectPurgeService,
    ProjectPurgeVectorStoreUnavailable,
    PurgeOutcome,
    create_project_purge_handler,
    register_project_purge_cron,
)
from gobby.runtime_grants.launch import ManagedLaunch


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
        return [SimpleNamespace(id="job-1", project_id=project_id, name="gobby:wiki-refresh")]

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


class FakeGwikiBarrier:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def drain(self, project_id: str, *, timeout: float) -> AsyncIterator[None]:
        del project_id, timeout
        self.events.append("gwiki:drain:enter")
        try:
            yield
        finally:
            self.events.append("gwiki:drain:exit")


class FakeWikiGateway:
    def __init__(self, events: list[str], *, success: bool = True) -> None:
        self.events = events
        self.success = success
        self.envs: list[Mapping[str, str] | None] = []

    async def purge_project_scope(
        self, project_id: str, *, timeout: float, env: Mapping[str, str] | None = None
    ) -> SimpleNamespace:
        del project_id, timeout
        self.events.append("wiki:purge")
        self.envs.append(env)
        return SimpleNamespace(success=self.success, stderr="wiki failed")


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
    wiki_success: bool = True,
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
        gwiki_barrier=FakeGwikiBarrier(events),
        wiki_gateway=FakeWikiGateway(events, success=wiki_success),
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
        "gwiki:drain:enter",
        "gwiki:drain:exit",
        "wiki:purge",
        "code:invalidate",
        "vectors:clear",
        "graph:clear",
        # Tombstone batches run one hub transaction per batch: the memory batch
        # deletes and tombstones, then each source kind drains with an empty SELECT.
        "hub:begin",
        "sql:memories",
        "hub:commit",
        "hub:begin",
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


def test_purge_cron_registration_preserves_disabled_state_and_wakes_enabled_null() -> None:
    class Executor:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def register_handler(self, name: str, handler: Any) -> None:
            self.handlers[name] = handler

    class Storage:
        def __init__(self) -> None:
            self.job = SimpleNamespace(
                id="job",
                name=PROJECT_PURGE_JOB_NAME,
                enabled=False,
                next_run_at=None,
                is_system=True,
            )
            self.woke = False

        def get_job_by_name(self, name: str) -> Any:
            assert name == PROJECT_PURGE_JOB_NAME
            return self.job

        def reconcile_system_job_definition(self, job_id: str, **fields: Any) -> Any:
            assert job_id == "job"
            assert fields["action_config"]["handler"] == PROJECT_PURGE_HANDLER_NAME
            return self.job

        def wake_system_job(self, job_id: str) -> Any:
            del job_id
            self.woke = True
            return self.job

    executor = Executor()
    storage = Storage()
    register_project_purge_cron(storage, executor, SimpleNamespace())

    assert PROJECT_PURGE_HANDLER_NAME in executor.handlers
    assert not storage.woke
    storage.job.enabled = True
    register_project_purge_cron(storage, executor, SimpleNamespace())
    assert storage.woke


@pytest.mark.asyncio
async def test_purge_runs_projection_cleanup_under_a_maintenance_launch() -> None:
    """A soft-deleted project has no checkout and is refused an interactive grant.

    gwiki purge and gcode invalidate therefore run inside a maintenance launch,
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
    assert factory.opened == ["p1", "p1"]
    wiki = cast(FakeWikiGateway, service.wiki_gateway)
    code = cast(FakeCodeGateway, service.code_gateway)
    assert wiki.envs == [{"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/grants/p1.json"}]
    assert code.envs == [{"GOBBY_MANAGED_EXECUTION_BOOTSTRAP": "/grants/p1.json"}]
    purge_window = events[events.index("gwiki:drain:exit") + 1 : events.index("vectors:clear")]
    assert purge_window == [
        "launch:open",
        "wiki:purge",
        "launch:close",
        "launch:open",
        "code:invalidate",
        "launch:close",
    ]


@pytest.mark.asyncio
async def test_purge_without_a_launch_factory_passes_no_grant_env() -> None:
    service, _projects, _vectors, _events = make_service(FakeProject("p1", "app"))

    result = await service.purge_project("p1")

    assert result.success
    assert cast(FakeWikiGateway, service.wiki_gateway).envs == [None]
    assert cast(FakeCodeGateway, service.code_gateway).envs == [None]
