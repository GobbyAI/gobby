from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.gwiki_gateway import GwikiGateway
from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import LocalProjectManager
from gobby.wiki.scheduled_jobs import (
    WIKI_RECAP_SCHEDULE_CRON,
    WIKI_SCHEDULED_GATEWAY_TIMEOUT_SECONDS,
    _create_gateway,
    _previous_utc_day,
    configured_wiki_cron_scopes,
    create_wiki_audit_handler,
    create_wiki_health_handler,
    create_wiki_librarian_handler,
    create_wiki_recap_handler,
    create_wiki_refresh_handler,
    create_wiki_sync_sessions_handler,
    create_wiki_upkeep_handler,
    register_wiki_cron_jobs,
)
from gobby.wiki.update_coordinator import WikiUpdateCoordinator

WIKI_JOB_COMMANDS = (
    "audit",
    "health",
    "librarian",
    "recap",
    "refresh",
    "sync-sessions",
    "upkeep",
)

PROJECT_ID = "proj-wiki"
pytestmark = pytest.mark.unit


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.index_calls = 0

    async def refresh(
        self,
        *,
        source_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("refresh", {"source_ids": source_ids, "dry_run": dry_run}))
        return _result(
            "refresh",
            {
                "status": "completed",
                "refreshed": [
                    {"raw_path": "raw/changed.md", "changed": True},
                    {"raw_path": "raw/unchanged.md", "changed": False},
                ],
                "indexed": {"documents": 1, "chunks": 3},
                "index_status": {"index_required": True},
            },
        )

    async def health(self) -> dict[str, Any]:
        self.calls.append(("health", {}))
        return _result("health", {"status": "healthy", "checked": 4})

    async def audit(self) -> dict[str, Any]:
        self.calls.append(("audit", {}))
        return _result(
            "audit",
            {
                "status": "completed",
                "findings": [],
                "changed_paths": ["audits/wiki-audit.md"],
            },
        )

    async def sync_sessions(
        self,
        *,
        archive_dir: str | Path | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("sync-sessions", {"archive_dir": archive_dir, "limit": limit}))
        return _result(
            "sync_sessions",
            {
                "status": "completed",
                "accepted": [{"raw_path": "sessions/session-1.md"}],
                "indexed": {"documents": 1},
            },
        )

    async def index(self) -> dict[str, Any]:
        self.index_calls += 1
        return _result("index", {"status": "indexed"})

    async def upkeep(self, *, dry_run: bool = False) -> dict[str, Any]:
        self.calls.append(("upkeep", {"dry_run": dry_run}))
        return _result(
            "upkeep",
            {
                "status": "completed",
                "command": "upkeep",
                "dry_run": dry_run,
                "pages_created": 1,
                "pages_updated": 1,
                "clusters": [
                    {"action": "created", "page_path": "knowledge/concepts/falkordb.md"},
                    {"action": "updated", "page_path": "knowledge/concepts/qdrant.md"},
                    {"action": "planned_create", "page_path": "knowledge/concepts/planned.md"},
                    {"action": "failed", "error": "synthesis timeout"},
                ],
            },
        )

    async def librarian(self) -> dict[str, Any]:
        self.calls.append(("librarian", {}))
        return _result(
            "librarian",
            {
                "command": "librarian",
                "checks": [{"name": "broken_links", "available": True, "items": []}],
                "suggested_tasks": [
                    {
                        "title": "Fix broken wikilink in gcode page",
                        "description": "Target missing.",
                        "paths": ["knowledge/concepts/gcode.md"],
                    },
                    {
                        "title": "Merge duplicate concept pages",
                        "description": "Two pages cover FalkorDB.",
                        "paths": [],
                    },
                    {
                        "title": "Fix broken wikilink in gcode page",
                        "description": "Duplicate suggestion in the same run.",
                        "paths": [],
                    },
                ],
                "artifacts": {"report": "meta/librarian/latest.md"},
            },
        )

    async def recap(self, *, date: str | None = None) -> dict[str, Any]:
        self.calls.append(("recap", {"date": date}))
        return _result(
            "recap",
            {
                "status": "completed",
                "command": "recap",
                "date": date,
                "sessions_selected": 2,
                "page_path": f"recaps/{date}.md",
                "page_action": "created",
            },
        )


class RecordingTaskManager:
    def __init__(self, open_titles_by_project: dict[str, list[str]] | None = None) -> None:
        self.open_titles_by_project = open_titles_by_project or {}
        self.created: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    def list_tasks(
        self,
        *,
        project_id: str | None = None,
        closed: bool | None = None,
        title_like: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        self.list_calls.append(
            {
                "project_id": project_id,
                "closed": closed,
                "title_like": title_like,
                "limit": limit,
            }
        )
        titles = self.open_titles_by_project.get(project_id or "", [])
        matching = [title for title in titles if title_like is None or title_like in title]
        return [SimpleNamespace(title=title) for title in matching]

    def create_task(
        self,
        project_id: str,
        title: str,
        description: str | None = None,
        *,
        labels: list[str] | None = None,
        category: str | None = None,
    ) -> object:
        self.created.append(
            {
                "project_id": project_id,
                "title": title,
                "description": description,
                "labels": labels,
                "category": category,
            }
        )
        self.open_titles_by_project.setdefault(project_id, []).append(title)
        return SimpleNamespace(title=title)


class LargeHealthGateway(RecordingGateway):
    def __init__(self, broken_links: list[dict[str, str]]) -> None:
        super().__init__()
        self.broken_links = broken_links

    async def health(self) -> dict[str, Any]:
        self.calls.append(("health", {}))
        return _result(
            "health",
            {
                "status": "degraded",
                "root": "/wiki/root",
                "scope": "project:alpha",
                "command": "health",
                "json_path": "meta/health/latest.json",
                "text_path": "meta/health/latest.md",
                "broken_links": self.broken_links,
                "stale_pages": [],
                "metadata": {"full": "details stay in health artifacts"},
            },
        )


class RecordingCoordinator:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    async def handle_write_result(self, result: dict[str, Any]) -> dict[str, Any]:
        self.results.append(result)
        coordinated = dict(result)
        coordinated["index_handoff"] = {"status": "completed"}
        return coordinated


@dataclass
class RecordingExecutor:
    handlers: dict[str, Any]

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


def _result(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "command": command, "payload": payload, "stderr": ""}


def _job(
    command: str,
    scope: str = "project:alpha",
    action_config: dict[str, Any] | None = None,
) -> CronJob:
    config = {"handler": f"wiki:{command}:{scope}", "scope": scope, **(action_config or {})}
    return CronJob(
        id=f"job-{command}",
        project_id=PROJECT_ID,
        name=f"gobby:wiki-{command}:{scope}",
        description=None,
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config=config,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def project_id(temp_db: Any) -> str:
    return LocalProjectManager(temp_db).create(name="wiki", repo_path="/tmp/wiki").id


@pytest.fixture
def cron_storage(temp_db: Any, project_id: str) -> CronJobStorage:
    return CronJobStorage(temp_db)


@pytest.mark.asyncio
async def test_cron_history_is_user_visible() -> None:
    gateway = RecordingGateway()
    handler = create_wiki_refresh_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("refresh")))

    assert output["purpose"] == "Refresh wiki sources"
    assert output["scope"] == "project:alpha"
    assert output["command"] == "refresh"
    assert output["status"] == "completed"
    assert output["ok"] is True
    assert "error" not in output
    assert output["changed_paths"] == ["raw/changed.md"]
    assert output["result"]["indexed"] == {"documents": 1, "chunks": 3}
    assert output["result"]["gwiki"]["command"] == "refresh"


@pytest.mark.asyncio
async def test_health_cron_history_summarizes_large_payloads() -> None:
    broken_links = [
        {"source": f"wiki/page-{index}.md", "target": f"missing-{index}.md"} for index in range(250)
    ]
    gateway = LargeHealthGateway(broken_links)
    handler = create_wiki_health_handler(gateway=gateway, scope="project:alpha")

    output = json.loads(await handler(_job("health")))

    assert output["purpose"] == "Run wiki health checks"
    assert output["scope"] == "project:alpha"
    assert output["command"] == "health"
    assert output["status"] == "degraded"
    assert output["ok"] is False
    assert output["error"] == "gwiki health reported status 'degraded'"
    assert output["result"]["status"] == "degraded"
    assert output["result"]["scope"] == "project:alpha"
    assert output["result"]["command"] == "health"
    assert output["result"]["root"] == "/wiki/root"
    assert output["result"]["json_path"] == "meta/health/latest.json"
    assert output["result"]["text_path"] == "meta/health/latest.md"
    assert output["result"]["broken_links_count"] == len(broken_links)
    assert output["result"]["broken_links_sample"] == broken_links[:10]
    assert len(output["result"]["broken_links_sample"]) == 10
    assert output["result"]["stale_pages_count"] == 0
    assert output["result"]["stale_pages_sample"] == []
    assert "broken_links" not in output["result"]
    assert "metadata" not in output["result"]
    assert output["result"]["gwiki"] == {"ok": True, "command": "health", "stderr": ""}


def _timeout_envelope(command: str) -> dict[str, Any]:
    return {
        "ok": False,
        "command": command,
        "status": "degraded",
        "payload": None,
        "stderr": "",
        "error": {"type": "timeout", "message": "gwiki command timed out"},
    }


class TimeoutRefreshGateway(RecordingGateway):
    async def refresh(
        self,
        *,
        source_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(("refresh", {"source_ids": source_ids, "dry_run": dry_run}))
        return _timeout_envelope("refresh")


@pytest.mark.asyncio
async def test_gwiki_timeout_envelope_marks_history_failed() -> None:
    gateway = TimeoutRefreshGateway()
    handler = create_wiki_refresh_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("refresh")))

    assert output["status"] == "degraded"
    assert output["ok"] is False
    assert output["error"] == "gwiki command timed out"


@pytest.mark.asyncio
async def test_gwiki_ok_false_envelope_marks_history_failed() -> None:
    class FailingAuditGateway(RecordingGateway):
        async def audit(self) -> dict[str, Any]:
            return {
                "ok": False,
                "command": "audit",
                "status": "failed",
                "payload": None,
                "stderr": "audit blew up",
                "error": {"type": "command", "returncode": 2, "message": "gwiki audit exited 2"},
            }

    gateway = FailingAuditGateway()
    handler = create_wiki_audit_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("audit")))

    assert output["status"] == "failed"
    assert output["ok"] is False
    assert output["error"] == "gwiki audit exited 2"


@pytest.mark.asyncio
async def test_gwiki_degraded_payload_status_marks_history_failed() -> None:
    class DegradedUpkeepGateway(RecordingGateway):
        async def upkeep(self, *, dry_run: bool = False) -> dict[str, Any]:
            return _result("upkeep", {"status": "degraded", "clusters": []})

    gateway = DegradedUpkeepGateway()
    handler = create_wiki_upkeep_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("upkeep")))

    assert output["status"] == "degraded"
    assert output["ok"] is False
    assert output["error"] == "gwiki upkeep reported status 'degraded'"


@pytest.mark.asyncio
async def test_recap_presync_failure_marks_history_failed() -> None:
    class PresyncTimeoutGateway(RecordingGateway):
        async def sync_sessions(
            self,
            *,
            archive_dir: str | Path | None = None,
            limit: int | None = None,
        ) -> dict[str, Any]:
            self.calls.append(("sync-sessions", {"archive_dir": archive_dir, "limit": limit}))
            return _timeout_envelope("sync-sessions")

    gateway = PresyncTimeoutGateway()
    handler = create_wiki_recap_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("recap")))

    assert output["command"] == "recap"
    assert output["status"] == "completed"
    assert output["ok"] is False
    assert output["error"] == "presync sync-sessions: gwiki command timed out"
    assert output["result"]["presync"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_gwiki_timeout_envelope_records_failed_cron_run(
    cron_storage: CronJobStorage, project_id: str
) -> None:
    """End-to-end: a gwiki timeout envelope must record a failed cron run with
    an error populated so consecutive_failures/backoff engage."""
    gateway = TimeoutRefreshGateway()
    handler = create_wiki_refresh_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )
    executor = CronExecutor(storage=cron_storage)
    executor.register_handler("wiki:refresh:project:alpha", handler)
    job = cron_storage.create_job(
        project_id=project_id,
        name="gobby:wiki-refresh:project:alpha",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={"handler": "wiki:refresh:project:alpha", "scope": "project:alpha"},
    )
    run = cron_storage.create_run(job.id)

    updated = await executor.execute(job, run)

    assert updated.status == "failed"
    assert updated.error == "gwiki command timed out"
    assert updated.output is not None
    assert json.loads(updated.output)["status"] == "degraded"


@pytest.mark.asyncio
async def test_scheduled_jobs_use_gateway() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    handlers = [
        (
            "refresh",
            create_wiki_refresh_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope="project:alpha",
            ),
        ),
        ("health", create_wiki_health_handler(gateway=gateway, scope="project:alpha")),
        (
            "audit",
            create_wiki_audit_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope="project:alpha",
            ),
        ),
        (
            "sync-sessions",
            create_wiki_sync_sessions_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope="project:alpha",
            ),
        ),
    ]

    handlers.extend(
        [
            (
                "upkeep",
                create_wiki_upkeep_handler(
                    gateway=gateway,
                    coordinator=coordinator,
                    scope="project:alpha",
                ),
            ),
            (
                "librarian",
                create_wiki_librarian_handler(
                    gateway=gateway,
                    scope="project:alpha",
                    task_manager=RecordingTaskManager(),
                    fallback_project_id=PROJECT_ID,
                ),
            ),
            (
                "recap",
                create_wiki_recap_handler(
                    gateway=gateway,
                    coordinator=coordinator,
                    scope="project:alpha",
                ),
            ),
        ]
    )

    for command, handler in handlers:
        output = json.loads(await handler(_job(command)))
        assert output["command"] == command

    assert [call[0] for call in gateway.calls] == [
        "refresh",
        "health",
        "audit",
        "sync-sessions",
        "upkeep",
        "librarian",
        "sync-sessions",
        "recap",
    ]


@pytest.mark.asyncio
async def test_wiki_cron_handlers_registered(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    executor = RecordingExecutor(handlers={})

    created = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=["project:alpha"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    repeated = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=["project:alpha"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    expected_handlers = {f"wiki:{command}:project:alpha" for command in WIKI_JOB_COMMANDS}
    assert set(executor.handlers) == expected_handlers
    assert created == 7
    assert repeated == 7

    jobs = cron_storage.list_jobs(project_id=project_id)
    assert sorted(job.name for job in jobs) == [
        f"gobby:wiki-{command}:project:alpha" for command in WIKI_JOB_COMMANDS
    ]
    assert all(job.action_type == "handler" for job in jobs)
    assert all(job.is_system for job in jobs)
    assert all(job.enabled for job in jobs)
    assert cron_storage.get_job_by_name("gobby:wiki-research:project:alpha") is None

    recap_job = cron_storage.get_job_by_name("gobby:wiki-recap:project:alpha")
    assert recap_job is not None
    assert recap_job.schedule_type == "cron"
    assert recap_job.cron_expr == WIKI_RECAP_SCHEDULE_CRON
    interval_jobs = [job for job in jobs if job.name != recap_job.name]
    assert all(job.schedule_type == "interval" for job in interval_jobs)


@pytest.mark.asyncio
async def test_wiki_cron_registration_requires_db_without_gateway_factory(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    executor = RecordingExecutor(handlers={})

    with pytest.raises(ValueError, match="requires db"):
        await register_wiki_cron_jobs(
            cron_storage=cron_storage,
            cron_executor=executor,
            project_id=project_id,
            scopes=["project:alpha"],
        )


@pytest.mark.asyncio
async def test_create_gateway_requires_db_without_gateway_factory() -> None:
    with pytest.raises(ValueError, match="requires db"):
        await _create_gateway("project:alpha", db=None, gateway_factory=None)


@pytest.mark.asyncio
async def test_create_gateway_uses_scheduled_timeout(
    project_id: str,
    temp_db: Any,
) -> None:
    gateway = await _create_gateway(f"project:{project_id}", db=temp_db, gateway_factory=None)

    assert isinstance(gateway, GwikiGateway)
    # Cron maintenance commands (librarian sweeps, upkeep/recap synthesis)
    # overrun the 30s interactive default.
    assert gateway._timeout_seconds == WIKI_SCHEDULED_GATEWAY_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_system_research_jobs_are_hard_deleted(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    existing = cron_storage.create_job(
        project_id=project_id,
        name="gobby:wiki-research:project:alpha",
        description="legacy default research",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={"handler": "wiki:research:project:alpha", "scope": "project:alpha"},
        enabled=True,
        is_system=True,
    )
    assert existing.next_run_at is not None

    for _ in range(2):
        await register_wiki_cron_jobs(
            cron_storage=cron_storage,
            cron_executor=RecordingExecutor(handlers={}),
            project_id=project_id,
            scopes=["project:alpha"],
            gateway_factory=lambda _scope: RecordingGateway(),
        )

    assert cron_storage.get_job(existing.id) is None
    remaining = [
        job
        for job in cron_storage.list_jobs(project_id=project_id)
        if job.name.startswith("gobby:wiki-research:")
    ]
    assert remaining == []


@pytest.mark.asyncio
async def test_query_backed_research_jobs_are_hard_deleted(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    query_job = cron_storage.create_job(
        project_id=project_id,
        name="gobby:wiki-research:project:alpha",
        description="explicit research",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={
            "handler": "wiki:research:project:alpha",
            "scope": "project:alpha",
            "query": "Fill citation gaps",
        },
        enabled=True,
        is_system=False,
    )
    executor = RecordingExecutor(handlers={})

    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=["project:alpha"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    assert cron_storage.get_job(query_job.id) is None
    assert "wiki:research:project:alpha" not in executor.handlers


@pytest.mark.asyncio
async def test_default_wiki_cron_scope_resolves_project_root(
    cron_storage: CronJobStorage,
    project_id: str,
    temp_db: Any,
) -> None:
    executor = RecordingExecutor(handlers={})
    resolved_scopes = []

    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        db=temp_db,
        scopes=configured_wiki_cron_scopes(None, project_id),
        gateway_factory=lambda scope: resolved_scopes.append(scope) or RecordingGateway(),
    )

    assert {scope.identity for scope in resolved_scopes} == {f"project:{project_id}"}
    assert {scope.project_root for scope in resolved_scopes} == {Path("/tmp/wiki").resolve()}
    assert sorted(job.name for job in cron_storage.list_jobs(project_id=project_id)) == [
        f"gobby:wiki-{command}:project:{project_id}" for command in WIKI_JOB_COMMANDS
    ]


@pytest.mark.asyncio
async def test_wiki_cron_registration_reconciles_bare_uuid_rows(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    legacy_refresh = cron_storage.create_job(
        project_id=project_id,
        name=f"gobby:wiki-refresh:{project_id}",
        description="legacy refresh",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={"handler": f"wiki:refresh:{project_id}"},
        enabled=True,
        is_system=True,
    )
    canonical_refresh = cron_storage.create_job(
        project_id=project_id,
        name=f"gobby:wiki-refresh:project:{project_id}",
        description="canonical refresh",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={"handler": f"wiki:refresh:project:{project_id}"},
        enabled=True,
        is_system=True,
    )
    cron_storage.create_job(
        project_id=project_id,
        name=f"gobby:wiki-research:{project_id}",
        description="legacy research",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={"handler": f"wiki:research:{project_id}"},
        enabled=True,
        is_system=True,
    )

    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=project_id,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    disabled_refresh = cron_storage.get_job(legacy_refresh.id)
    assert disabled_refresh is not None
    assert disabled_refresh.name == f"gobby:wiki-refresh:{project_id}"
    assert disabled_refresh.enabled is False
    assert disabled_refresh.next_run_at is None
    assert cron_storage.get_job(canonical_refresh.id) is not None
    assert cron_storage.get_job_by_name(f"gobby:wiki-research:{project_id}") is None
    assert cron_storage.get_job_by_name(f"gobby:wiki-research:project:{project_id}") is None


@pytest.mark.asyncio
async def test_refresh_job_uses_gateway_and_avoids_duplicate_index() -> None:
    gateway = RecordingGateway()
    handler = create_wiki_refresh_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("refresh")))

    assert gateway.calls == [("refresh", {"source_ids": None, "dry_run": False})]
    assert gateway.index_calls == 0
    assert output["changed_paths"] == ["raw/changed.md"]
    assert output["result"]["indexed"] == {"documents": 1, "chunks": 3}
    assert output["result"]["index_handoff"] == {
        "status": "skipped",
        "reason": "cli_indexed_batch",
    }


@pytest.mark.asyncio
async def test_audit_job_routes_through_gateway_audit() -> None:
    gateway = RecordingGateway()
    coordinator = RecordingCoordinator()
    handler = create_wiki_audit_handler(
        gateway=gateway,
        coordinator=coordinator,
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("audit")))

    assert gateway.calls == [("audit", {})]
    assert len(coordinator.results) == 1
    assert output["command"] == "audit"
    assert output["changed_paths"] == ["audits/wiki-audit.md"]
    assert output["result"]["gwiki"]["command"] == "audit"


@pytest.mark.asyncio
async def test_upkeep_job_routes_through_write_coordinator() -> None:
    gateway = RecordingGateway()
    coordinator = RecordingCoordinator()
    handler = create_wiki_upkeep_handler(
        gateway=gateway,
        coordinator=coordinator,
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("upkeep")))

    assert gateway.calls == [("upkeep", {"dry_run": False})]
    assert len(coordinator.results) == 1
    assert coordinator.results[0]["command"] == "upkeep"
    assert output["command"] == "upkeep"
    # Only clusters that actually wrote pages count; planned/failed are excluded.
    assert output["changed_paths"] == [
        "knowledge/concepts/falkordb.md",
        "knowledge/concepts/qdrant.md",
    ]
    assert output["result"]["index_handoff"] == {"status": "completed"}


@pytest.mark.asyncio
async def test_recap_job_presyncs_sessions_and_routes_through_write_coordinator() -> None:
    gateway = RecordingGateway()
    coordinator = RecordingCoordinator()
    handler = create_wiki_recap_handler(
        gateway=gateway,
        coordinator=coordinator,
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("recap")))

    expected_date = _previous_utc_day()
    assert [call[0] for call in gateway.calls] == ["sync-sessions", "recap"]
    assert gateway.calls[1] == ("recap", {"date": expected_date})
    assert [result["command"] for result in coordinator.results] == ["sync_sessions", "recap"]
    assert output["command"] == "recap"
    assert output["changed_paths"] == [f"recaps/{expected_date}.md"]
    assert output["result"]["presync"] == {"command": "sync-sessions", "status": "completed"}


@pytest.mark.asyncio
async def test_librarian_job_files_deduped_tasks_without_write_coordinator() -> None:
    gateway = RecordingGateway()
    task_manager = RecordingTaskManager(
        open_titles_by_project={"alpha": ["Merge duplicate concept pages"]}
    )
    handler = create_wiki_librarian_handler(
        gateway=gateway,
        scope="project:alpha",
        task_manager=task_manager,
        fallback_project_id=PROJECT_ID,
    )

    output = json.loads(await handler(_job("librarian")))

    assert gateway.calls == [("librarian", {})]
    # Librarian is read-only for the coordinator boundary: it only writes
    # watcher-ignored meta/librarian artifacts, so no index handoff runs.
    assert gateway.index_calls == 0
    assert "index_handoff" not in output["result"]

    # History is compacted: raw suggestion/check payloads overflow the cron
    # run output budget.
    assert output["result"]["suggested_tasks_count"] == 3
    assert "suggested_tasks" not in output["result"]
    assert output["result"]["checks"] == [
        {"name": "broken_links", "available": True, "items_count": 0, "items_sample": []}
    ]
    assert "payload" not in output["result"]["gwiki"]

    assert [task["title"] for task in task_manager.created] == ["Fix broken wikilink in gcode page"]
    created = task_manager.created[0]
    assert created["project_id"] == "alpha"
    assert created["labels"] == ["wiki-librarian:project:alpha"]
    assert created["category"] == "docs"
    assert "knowledge/concepts/gcode.md" in created["description"]

    filing = output["result"]["task_filing"]
    assert filing["status"] == "completed"
    assert filing["filed"] == 1
    assert filing["deduplicated"] == 2
    assert filing["titles"] == ["Fix broken wikilink in gcode page"]


@pytest.mark.asyncio
async def test_librarian_job_reports_unavailable_task_manager() -> None:
    gateway = RecordingGateway()
    handler = create_wiki_librarian_handler(
        gateway=gateway,
        scope="project:alpha",
        task_manager=None,
        fallback_project_id=PROJECT_ID,
    )

    output = json.loads(await handler(_job("librarian")))

    filing = output["result"]["task_filing"]
    assert filing["status"] == "unavailable"
    assert filing["filed"] == 0
    assert filing["suggested"] == 3


@pytest.mark.asyncio
async def test_multi_scope_registration_files_per_scope_tasks(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    executor = RecordingExecutor(handlers={})
    task_manager = RecordingTaskManager()
    gateways: dict[str, RecordingGateway] = {}

    def gateway_factory(scope: Any) -> RecordingGateway:
        gateway = RecordingGateway()
        gateways[scope.identity if hasattr(scope, "identity") else str(scope)] = gateway
        return gateway

    created = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=["project:alpha", "topic:sessions"],
        gateway_factory=gateway_factory,
        task_manager=task_manager,
    )

    assert created == 14
    job_names = sorted(job.name for job in cron_storage.list_jobs(project_id=project_id))
    assert job_names == sorted(
        [f"gobby:wiki-{command}:project:alpha" for command in WIKI_JOB_COMMANDS]
        + [f"gobby:wiki-{command}:topic:sessions" for command in WIKI_JOB_COMMANDS]
    )

    for scope in ("project:alpha", "topic:sessions"):
        output = json.loads(await executor.handlers[f"wiki:librarian:{scope}"](_job("librarian")))
        assert output["scope"] == scope
        assert output["result"]["task_filing"]["filed"] >= 1

    by_label = {task["labels"][0]: task for task in task_manager.created}
    alpha_task = by_label["wiki-librarian:project:alpha"]
    sessions_task = by_label["wiki-librarian:topic:sessions"]
    # Project scopes file into their own project; topic scopes fall back to
    # the registering project (cross-project sessions -> topic:sessions).
    assert alpha_task["project_id"] == "alpha"
    assert sessions_task["project_id"] == project_id
    assert {task["category"] for task in task_manager.created} == {"docs"}

    for scope, gateway in gateways.items():
        assert ("librarian", {}) in gateway.calls, scope
