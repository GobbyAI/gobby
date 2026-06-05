from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import LocalProjectManager
from gobby.wiki.scheduled_jobs import (
    _create_gateway,
    configured_wiki_cron_scopes,
    create_wiki_audit_handler,
    create_wiki_health_handler,
    create_wiki_refresh_handler,
    create_wiki_research_handler,
    register_wiki_cron_jobs,
)
from gobby.wiki.update_coordinator import WikiUpdateCoordinator

PROJECT_ID = "proj-wiki"
pytestmark = pytest.mark.unit


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.index_calls = 0

    async def research(
        self,
        query: str | None = None,
        *,
        audit: bool = False,
        source_constraints: list[str] | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        max_sources: int | None = None,
        ai: str | None = None,
        require_ai: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "research",
                {
                    "query": query,
                    "audit": audit,
                    "source_constraints": source_constraints,
                    "max_steps": max_steps,
                    "max_tokens": max_tokens,
                    "max_sources": max_sources,
                    "ai": ai,
                    "require_ai": require_ai,
                },
            )
        )
        payload: dict[str, Any] = {"status": "completed", "items": 2}
        if audit:
            payload = {
                "status": "completed",
                "findings": [],
                "changed_paths": ["audits/wiki-audit.md"],
            }
        return _result("research", payload)

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
        return _result("audit", {"status": "passed", "issues": []})

    async def index(self) -> dict[str, Any]:
        self.index_calls += 1
        return _result("index", {"status": "indexed"})


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
    assert output["changed_paths"] == ["raw/changed.md"]
    assert output["result"]["indexed"] == {"documents": 1, "chunks": 3}
    assert output["result"]["gwiki"]["command"] == "refresh"


@pytest.mark.asyncio
async def test_scheduled_jobs_use_gateway() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    handlers = [
        (
            "research",
            create_wiki_research_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope="project:alpha",
            ),
        ),
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
    ]

    for command, handler in handlers:
        action_config = {"query": "Find stale wiki gaps"} if command == "research" else None
        output = json.loads(await handler(_job(command, action_config=action_config)))
        assert output["command"] == command

    assert [call[0] for call in gateway.calls] == ["research", "refresh", "health", "research"]
    assert gateway.calls[0][1]["query"] == "Find stale wiki gaps"
    assert gateway.calls[0][1]["ai"] == "daemon"
    assert gateway.calls[-1][1]["audit"] is True
    assert gateway.calls[-1][1]["ai"] == "daemon"


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

    expected_handlers = {
        "wiki:research:project:alpha",
        "wiki:refresh:project:alpha",
        "wiki:health:project:alpha",
        "wiki:audit:project:alpha",
    }
    assert set(executor.handlers) == expected_handlers
    assert created == 4
    assert repeated == 4

    jobs = cron_storage.list_jobs(project_id=project_id)
    assert sorted(job.name for job in jobs) == [
        "gobby:wiki-audit:project:alpha",
        "gobby:wiki-health:project:alpha",
        "gobby:wiki-refresh:project:alpha",
    ]
    assert all(job.action_type == "handler" for job in jobs)
    assert all(job.is_system for job in jobs)
    assert cron_storage.get_job_by_name("gobby:wiki-research:project:alpha") is None


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
async def test_queryless_system_research_jobs_are_retired(
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

    retired = cron_storage.get_job(existing.id)
    assert retired is not None
    assert retired.enabled is False
    assert retired.next_run_at is None


@pytest.mark.asyncio
async def test_query_backed_research_jobs_are_preserved_and_routed(
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
    gateway = RecordingGateway()
    executor = RecordingExecutor(handlers={})

    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=["project:alpha"],
        gateway_factory=lambda _scope: gateway,
    )

    preserved = cron_storage.get_job(query_job.id)
    assert preserved is not None
    assert preserved.enabled is True
    assert preserved.action_config["query"] == "Fill citation gaps"

    output = json.loads(await executor.handlers["wiki:research:project:alpha"](preserved))

    assert gateway.calls[0][1]["query"] == "Fill citation gaps"
    assert gateway.calls[0][1]["ai"] == "daemon"
    assert output["command"] == "research"


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
        f"gobby:wiki-audit:project:{project_id}",
        f"gobby:wiki-health:project:{project_id}",
        f"gobby:wiki-refresh:project:{project_id}",
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
    retired_research = cron_storage.get_job_by_name(f"gobby:wiki-research:project:{project_id}")
    assert retired_research is not None
    assert retired_research.enabled is False
    assert retired_research.next_run_at is None


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
async def test_research_job_routes_write_result_through_coordinator() -> None:
    gateway = RecordingGateway()
    coordinator = RecordingCoordinator()
    handler = create_wiki_research_handler(
        gateway=gateway,
        coordinator=coordinator,
        scope="project:alpha",
    )

    output = json.loads(
        await handler(_job("research", action_config={"query": "Fill citation gaps"}))
    )

    assert gateway.calls == [
        (
            "research",
            {
                "query": "Fill citation gaps",
                "audit": False,
                "source_constraints": None,
                "max_steps": None,
                "max_tokens": None,
                "max_sources": None,
                "ai": "daemon",
                "require_ai": False,
            },
        )
    ]
    assert len(coordinator.results) == 1
    assert coordinator.results[0]["payload"] == {"status": "completed", "items": 2}
    assert output["result"]["index_handoff"] == {"status": "completed"}


@pytest.mark.asyncio
async def test_queryless_research_job_fails_before_gateway_call() -> None:
    gateway = RecordingGateway()
    handler = create_wiki_research_handler(
        gateway=gateway,
        coordinator=RecordingCoordinator(),
        scope="project:alpha",
    )

    with pytest.raises(ValueError, match="action_config.query"):
        await handler(_job("research"))

    assert gateway.calls == []


@pytest.mark.asyncio
async def test_audit_job_routes_through_research_audit_mode() -> None:
    gateway = RecordingGateway()
    coordinator = RecordingCoordinator()
    handler = create_wiki_audit_handler(
        gateway=gateway,
        coordinator=coordinator,
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("audit")))

    assert gateway.calls == [
        (
            "research",
            {
                "query": None,
                "audit": True,
                "source_constraints": None,
                "max_steps": None,
                "max_tokens": None,
                "max_sources": None,
                "ai": "daemon",
                "require_ai": False,
            },
        )
    ]
    assert len(coordinator.results) == 1
    assert output["command"] == "audit"
    assert output["changed_paths"] == ["audits/wiki-audit.md"]
    assert output["result"]["gwiki"]["command"] == "research"
