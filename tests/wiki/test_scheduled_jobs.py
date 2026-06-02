from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import LocalProjectManager
from gobby.wiki.scheduled_jobs import (
    create_wiki_audit_handler,
    create_wiki_health_handler,
    create_wiki_refresh_handler,
    create_wiki_research_handler,
    register_wiki_cron_jobs,
)
from gobby.wiki.update_coordinator import WikiUpdateCoordinator

PROJECT_ID = "proj-wiki"


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.index_calls = 0

    async def research(self, query: str | None = None) -> dict[str, Any]:
        self.calls.append(("research", {"query": query}))
        return _result("research", {"status": "completed", "items": 2})

    async def refresh(
        self,
        *,
        scope: str | None = None,
        source_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            ("refresh", {"scope": scope, "source_ids": source_ids, "dry_run": dry_run})
        )
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


@dataclass
class RecordingExecutor:
    handlers: dict[str, Any]

    def register_handler(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


def _result(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "command": command, "payload": payload, "stderr": ""}


def _job(command: str, scope: str = "project:alpha") -> CronJob:
    return CronJob(
        id=f"job-{command}",
        project_id=PROJECT_ID,
        name=f"gobby:wiki-{command}:{scope}",
        description=None,
        schedule_type="interval",
        interval_seconds=3600,
        action_type="handler",
        action_config={"handler": f"wiki:{command}:{scope}", "scope": scope},
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def project_id(temp_db: Any) -> str:
    return LocalProjectManager(temp_db).create(name="wiki", repo_path="/tmp/wiki").id


@pytest.fixture
def cron_storage(temp_db: Any, project_id: str) -> CronJobStorage:
    return CronJobStorage(temp_db)


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


async def test_scheduled_jobs_use_gateway() -> None:
    gateway = RecordingGateway()
    coordinator = WikiUpdateCoordinator(gateway)

    handlers = [
        ("research", create_wiki_research_handler(gateway=gateway, scope="project:alpha")),
        (
            "refresh",
            create_wiki_refresh_handler(
                gateway=gateway,
                coordinator=coordinator,
                scope="project:alpha",
            ),
        ),
        ("health", create_wiki_health_handler(gateway=gateway, scope="project:alpha")),
        ("audit", create_wiki_audit_handler(gateway=gateway, scope="project:alpha")),
    ]

    for command, handler in handlers:
        output = json.loads(await handler(_job(command)))
        assert output["command"] == command

    assert [call[0] for call in gateway.calls] == ["research", "refresh", "health", "audit"]


def test_wiki_cron_handlers_registered(cron_storage: CronJobStorage, project_id: str) -> None:
    executor = RecordingExecutor(handlers={})

    created = register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=["project:alpha"],
    )
    repeated = register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=["project:alpha"],
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
        "gobby:wiki-research:project:alpha",
    ]
    assert all(job.action_type == "handler" for job in jobs)
    assert all(job.is_system for job in jobs)


async def test_refresh_job_uses_gateway_and_avoids_duplicate_index() -> None:
    gateway = RecordingGateway()
    handler = create_wiki_refresh_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("refresh")))

    assert gateway.calls == [
        ("refresh", {"scope": "project:alpha", "source_ids": None, "dry_run": False})
    ]
    assert gateway.index_calls == 0
    assert output["changed_paths"] == ["raw/changed.md"]
    assert output["result"]["indexed"] == {"documents": 1, "chunks": 3}
    assert output["result"]["index_handoff"] == {
        "status": "skipped",
        "reason": "cli_indexed_batch",
    }
