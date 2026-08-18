from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from gobby.config.cron import CronConfig
from gobby.gwiki_gateway import GwikiGateway
from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import LocalProjectManager
from gobby.wiki.scheduled_jobs import (
    WIKI_RECAP_SCHEDULE_CRON,
    WIKI_SCHEDULED_GATEWAY_TIMEOUT_SECONDS,
    WIKI_UPKEEP_TIME_BUDGET_SECONDS,
    _create_gateway,
    _previous_utc_day,
    create_wiki_audit_handler,
    create_wiki_exports_handler,
    create_wiki_health_handler,
    create_wiki_librarian_handler,
    create_wiki_recap_handler,
    create_wiki_refresh_handler,
    create_wiki_sync_sessions_handler,
    create_wiki_upkeep_handler,
    register_wiki_cron_jobs,
)
from gobby.wiki.scope_resolution import ResolvedWikiScope
from gobby.wiki.update_coordinator import WikiUpdateCoordinator

WIKI_JOB_COMMANDS = (
    "audit",
    "exports",
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

    async def export_pages(self) -> dict[str, Any]:
        self.calls.append(("export_pages", {}))
        return _result("export_pages", {"status": "completed"})

    async def graph_artifacts(self) -> dict[str, Any]:
        self.calls.append(("graph_artifacts", {}))
        return _result("graph_artifacts", {"status": "completed"})

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

    async def upkeep(
        self,
        *,
        dry_run: bool = False,
        ai: str | None = None,
        max_pages: int | None = None,
        time_budget_seconds: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "upkeep",
                {
                    "dry_run": dry_run,
                    "ai": ai,
                    "max_pages": max_pages,
                    "time_budget_seconds": time_budget_seconds,
                },
            )
        )
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
        validation_criteria: str | None = None,
    ) -> object:
        self.created.append(
            {
                "project_id": project_id,
                "title": title,
                "description": description,
                "labels": labels,
                "category": category,
                "validation_criteria": validation_criteria,
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
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
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
    assert "payload" not in output["result"]["gwiki"]


class LargeRefreshGateway(RecordingGateway):
    """Refresh result shaped like a real bulk sweep over a large catalog."""

    def __init__(self, *, skipped: list[dict[str, Any]], failed: list[dict[str, Any]]) -> None:
        super().__init__()
        self.skipped = skipped
        self.failed = failed

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
                "status": "unchanged",
                "planned": [{"id": f"src-planned-{index}"} for index in range(60)],
                "refreshed": [{"raw_path": "raw/changed.md", "changed": True}],
                "unchanged": [
                    {"id": f"src-unchanged-{index}", "raw_path": f"raw/source-{index}.md"}
                    for index in range(60)
                ],
                "failed": self.failed,
                "skipped": self.skipped,
                "indexed": {"documents": 1, "chunks": 3},
                "index_status": {"index_required": False},
            },
        )


class LargeAuditGateway(RecordingGateway):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    async def audit(self) -> dict[str, Any]:
        self.calls.append(("audit", {}))
        return _result("audit", self.payload)


class LargeSyncSessionsGateway(RecordingGateway):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload

    async def sync_sessions(
        self,
        *,
        archive_dir: str | Path | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("sync-sessions", {"archive_dir": archive_dir, "limit": limit}))
        return _result("sync_sessions", self.payload)


@pytest.mark.asyncio
async def test_refresh_cron_history_compacts_per_source_arrays() -> None:
    skipped = [
        {
            "id": f"src-{index}-session",
            "location": f"session:{index}",
            "source_kind": "session",
            "code": "missing_replay_metadata",
            "message": (
                f"source `src-{index}-session` has kind `session` but no local replay metadata"
            ),
        }
        for index in range(213)
    ]
    failed = [
        {"id": f"src-broken-{index}", "code": "replay_failed", "message": "replay error"}
        for index in range(7)
    ]
    gateway = LargeRefreshGateway(skipped=skipped, failed=failed)
    handler = create_wiki_refresh_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    raw_output = await handler(_job("refresh"))
    output = json.loads(raw_output)

    result = output["result"]
    assert result["planned_count"] == 60
    assert "planned" not in result
    assert result["unchanged_count"] == 60
    assert "unchanged" not in result
    assert result["refreshed"] == [{"raw_path": "raw/changed.md", "changed": True}]
    assert result["skipped_count"] == 213
    assert result["skipped"] == [
        {
            "code": "missing_replay_metadata",
            "count": 213,
            "sample": skipped[:5],
        }
    ]
    assert result["failed_count"] == 7
    assert result["failed"] == [{"code": "replay_failed", "count": 7, "sample": failed[:5]}]
    assert "payload" not in result["gwiki"]
    assert len(raw_output) < 10_000


@pytest.mark.asyncio
async def test_audit_cron_history_compacts_verbose_collections() -> None:
    source_context = [
        {
            "source_id": f"src-{index}",
            "path": f"knowledge/sources/source-{index}.md",
            "citation": f"session:{index}",
            "location": f"session:{index}",
        }
        for index in range(250)
    ]
    claims = [
        {
            "path": "knowledge/concepts/gobby.md",
            "line": index,
            "heading": "Overview",
            "claim": f"Claim {index}: " + ("detailed evidence " * 300),
            "classification": "EXTRACTED",
        }
        for index in range(120)
    ]
    unsupported_claims = [
        {
            "path": "knowledge/concepts/gobby.md",
            "line": index,
            "heading": "Overview",
            "claim": f"Unsupported claim {index}: " + ("missing evidence " * 300),
            "reason": "No supporting source found",
            "source_context": source_context,
        }
        for index in range(40)
    ]
    gateway = LargeAuditGateway(
        {
            "status": "completed",
            "root": "/wiki/root",
            "claims": claims,
            "unsupported_claims": unsupported_claims,
            "source_context": source_context,
        }
    )
    handler = create_wiki_audit_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    raw_output = await handler(_job("audit"))
    output = json.loads(raw_output)
    result = output["result"]

    assert result["root"] == "/wiki/root"
    assert result["claims_count"] == 120
    assert len(result["claims_sample"]) == 3
    assert result["claims_sample"][0]["claim"].endswith("...")
    assert result["unsupported_claims_count"] == 40
    assert result["unsupported_claims_sample"][0]["source_context_count"] == 250
    assert result["source_context_count"] == 250
    assert len(result["source_context_sample"]) == 3
    assert result["gwiki"] == {"ok": True, "command": "audit", "stderr": ""}
    assert "claims" not in result
    assert "unsupported_claims" not in result
    assert "source_context" not in result
    assert len(raw_output) < 10_000


@pytest.mark.asyncio
async def test_sync_sessions_cron_history_compacts_per_session_results() -> None:
    accepted = [
        {
            "archive_path": f"/archive/session-{index}.md",
            "raw_path": f"raw/session-{index}.md",
            "source": {
                "id": f"src-{index}",
                "kind": "session",
                "location": f"session:{index}",
                "content_hash": f"hash-{index}",
            },
        }
        for index in range(25)
    ]
    skipped = [
        {
            "archive_path": f"/archive/skipped-{index}.md",
            "content_hash": f"skipped-hash-{index}",
            "reason": "content_hash_already_ingested",
        }
        for index in range(4_200)
    ]
    reconciled = [
        {
            "source_id": f"src-reconciled-{index}",
            "canonical_location": f"session:reconciled-{index}",
            "content_hash": f"reconciled-hash-{index}",
        }
        for index in range(60)
    ]
    gateway = LargeSyncSessionsGateway(
        {
            "status": "completed",
            "archive_dir": "/archive",
            "scanned": 4_285,
            "accepted": accepted,
            "skipped": skipped,
            "failed": [],
            "reconciled": reconciled,
            "indexed": {"documents": 323, "chunks": 2_416, "links": 847},
        }
    )
    handler = create_wiki_sync_sessions_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )

    raw_output = await handler(_job("sync-sessions"))
    output = json.loads(raw_output)
    result = output["result"]

    assert result["archive_dir"] == "/archive"
    assert result["scanned"] == 4_285
    assert result["indexed"] == {"documents": 323, "chunks": 2_416, "links": 847}
    assert result["accepted_count"] == 25
    assert result["accepted_sample"] == accepted[:3]
    assert result["skipped_count"] == 4_200
    assert result["skipped_sample"] == skipped[:3]
    assert result["failed_count"] == 0
    assert result["failed_sample"] == []
    assert result["reconciled_count"] == 60
    assert result["reconciled_sample"] == reconciled[:3]
    assert result["gwiki"] == {"ok": True, "command": "sync_sessions", "stderr": ""}
    assert "accepted" not in result
    assert "skipped" not in result
    assert "failed" not in result
    assert "reconciled" not in result
    assert len(raw_output) < 10_000


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
async def test_degraded_index_handoff_marks_history_failed() -> None:
    class DegradedIndexCoordinator:
        async def handle_write_result(self, result: dict[str, Any]) -> dict[str, Any]:
            return {
                **result,
                "index_handoff": {
                    "status": "degraded",
                    "degradation": {
                        "type": "index_handoff_failed",
                        "message": "gwiki index timed out",
                    },
                },
            }

    gateway = RecordingGateway()
    handler = create_wiki_refresh_handler(
        gateway=gateway,
        coordinator=DegradedIndexCoordinator(),
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("refresh")))

    assert output["status"] == "completed"
    assert output["ok"] is False
    assert output["error"] == "index handoff degraded: gwiki index timed out"
    assert output["result"]["index_handoff"]["status"] == "degraded"


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
        async def upkeep(
            self,
            *,
            dry_run: bool = False,
            ai: str | None = None,
            max_pages: int | None = None,
            time_budget_seconds: int | None = None,
        ) -> dict[str, Any]:
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
    assert run is not None

    updated = await executor.execute(job, run)

    assert updated.status == "failed"
    assert updated.error == "gwiki command timed out"
    assert updated.output is not None
    assert json.loads(updated.output)["status"] == "degraded"


async def test_upkeep_synthesis_failures_record_failed_cron_run(
    cron_storage: CronJobStorage, project_id: str
) -> None:
    class FailedSynthesisGateway(RecordingGateway):
        async def upkeep(
            self,
            *,
            dry_run: bool = False,
            ai: str | None = None,
            max_pages: int | None = None,
            time_budget_seconds: int | None = None,
        ) -> dict[str, Any]:
            return _result(
                "upkeep",
                {
                    "status": "completed",
                    "failures": 2,
                    "clusters": [
                        {"action": "failed", "error": "daemon candidate timed out"},
                        {"action": "failed", "error": "provider unavailable"},
                    ],
                },
            )

    gateway = FailedSynthesisGateway()
    handler = create_wiki_upkeep_handler(
        gateway=gateway,
        coordinator=WikiUpdateCoordinator(gateway),
        scope="project:alpha",
    )
    executor = CronExecutor(storage=cron_storage)
    executor.register_handler("wiki:upkeep:project:alpha", handler)
    job = cron_storage.create_job(
        project_id=project_id,
        name="gobby:wiki-upkeep:project:alpha",
        schedule_type="interval",
        interval_seconds=86_400,
        action_type="handler",
        action_config={"handler": "wiki:upkeep:project:alpha", "scope": "project:alpha"},
    )
    run = cron_storage.create_run(job.id)
    assert run is not None

    updated = await executor.execute(job, run)

    assert updated.status == "failed"
    assert updated.error == (
        "gwiki upkeep reported 2 synthesis failures: "
        "daemon candidate timed out; provider unavailable"
    )
    assert updated.output is not None
    output = json.loads(updated.output)
    assert output["result"]["failures"] == 2
    assert len(output["result"]["clusters"]) == 2


class FailingExportsGateway(RecordingGateway):
    def __init__(self, failed_steps: set[str]) -> None:
        super().__init__()
        self.failed_steps = failed_steps

    async def export_pages(self) -> dict[str, Any]:
        self.calls.append(("export_pages", {}))
        if "export_pages" in self.failed_steps:
            return {"ok": False, "command": "export_pages", "error": "pages failed"}
        return _result("export_pages", {"status": "completed"})

    async def graph_artifacts(self) -> dict[str, Any]:
        self.calls.append(("graph_artifacts", {}))
        if "graph_artifacts" in self.failed_steps:
            raise RuntimeError("graph failed")
        return _result("graph_artifacts", {"status": "completed"})


@pytest.mark.asyncio
async def test_exports_handler_records_per_step_success() -> None:
    gateway = RecordingGateway()
    handler = create_wiki_exports_handler(
        gateway=gateway,
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("exports")))

    assert gateway.calls == [("export_pages", {}), ("graph_artifacts", {})]
    assert output == {
        "command": "exports",
        "ok": True,
        "purpose": "Refresh agent-facing wiki exports",
        "result": {
            "steps": {
                "export_pages": {"ok": True, "status": "completed"},
                "graph_artifacts": {"ok": True, "status": "completed"},
            }
        },
        "scope": "project:alpha",
        "status": "completed",
    }


@pytest.mark.parametrize(
    ("failed_step", "expected_error"),
    [("export_pages", "pages failed"), ("graph_artifacts", "graph failed")],
)
@pytest.mark.asyncio
async def test_exports_handler_degrades_after_one_failed_step(
    failed_step: str,
    expected_error: str,
) -> None:
    gateway = FailingExportsGateway({failed_step})
    handler = create_wiki_exports_handler(
        gateway=gateway,
        scope="project:alpha",
    )

    output = json.loads(await handler(_job("exports")))

    assert [call[0] for call in gateway.calls] == ["export_pages", "graph_artifacts"]
    assert output["ok"] is True
    assert output["status"] == "degraded"
    steps = output["result"]["steps"]
    assert steps[failed_step]["ok"] is False
    assert steps[failed_step]["error"] == expected_error
    successful_step = ({"export_pages", "graph_artifacts"} - {failed_step}).pop()
    assert steps[successful_step] == {"ok": True, "status": "completed"}


@pytest.mark.asyncio
async def test_exports_handler_raises_after_both_steps_fail() -> None:
    gateway = FailingExportsGateway({"export_pages", "graph_artifacts"})
    handler = create_wiki_exports_handler(
        gateway=gateway,
        scope="project:alpha",
    )

    with pytest.raises(RuntimeError, match="pages failed.*graph failed"):
        await handler(_job("exports"))

    assert [call[0] for call in gateway.calls] == ["export_pages", "graph_artifacts"]


@pytest.mark.asyncio
async def test_remote_topic_gateway_does_not_construct_local_gwiki(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.paths import get_gobby_home
    from gobby.wiki.owner_gateway import RemoteWikiGateway
    from gobby.wiki.scheduled_jobs import _gateway_for_resolved

    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    bootstrap = get_gobby_home() / "bootstrap.yaml"
    bootstrap.write_text(
        "datastore_mode: remote\nhub_daemon_url: http://hub.example.test:60887\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("local GwikiGateway must not be constructed")

    monkeypatch.setattr("gobby.wiki.owner_dispatch.GwikiGateway", boom)
    gateway = _gateway_for_resolved(
        ResolvedWikiScope(identity="topic:research", topic="research"),
        None,
    )
    assert isinstance(gateway, RemoteWikiGateway)


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
            "exports",
            create_wiki_exports_handler(
                gateway=gateway,
                scope="project:alpha",
            ),
        ),
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
        "export_pages",
        "graph_artifacts",
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
    assert created == len(WIKI_JOB_COMMANDS)
    assert repeated == len(WIKI_JOB_COMMANDS)

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
    exports_job = cron_storage.get_job_by_name("gobby:wiki-exports:project:alpha")
    assert exports_job is not None
    assert exports_job.interval_seconds == 6 * 60 * 60


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


def test_upkeep_deadline_hierarchy_has_distinct_grace_windows() -> None:
    config = CronConfig()

    assert WIKI_UPKEEP_TIME_BUDGET_SECONDS == 1320
    assert WIKI_SCHEDULED_GATEWAY_TIMEOUT_SECONDS == 1380
    assert config.running_timeout_seconds == 1440
    assert config.stale_run_timeout_seconds == 1560
    assert (
        WIKI_UPKEEP_TIME_BUDGET_SECONDS
        < WIKI_SCHEDULED_GATEWAY_TIMEOUT_SECONDS
        < config.running_timeout_seconds
        < config.stale_run_timeout_seconds
    )


@pytest.mark.asyncio
async def test_system_research_jobs_are_left_untouched(
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

    remaining = cron_storage.get_job(existing.id)
    assert remaining is not None
    assert remaining.enabled is True


@pytest.mark.asyncio
async def test_query_backed_research_jobs_are_left_untouched(
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

    assert cron_storage.get_job(query_job.id) is not None
    assert "wiki:research:project:alpha" not in executor.handlers


@pytest.mark.asyncio
async def test_default_wiki_cron_scope_resolves_project_root(
    cron_storage: CronJobStorage,
    project_id: str,
    temp_db: Any,
) -> None:
    executor = RecordingExecutor(handlers={})
    resolved_scopes: list[ResolvedWikiScope] = []

    def gateway_factory(scope: ResolvedWikiScope) -> RecordingGateway:
        resolved_scopes.append(scope)
        return RecordingGateway()

    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        db=temp_db,
        scopes=None,
        gateway_factory=gateway_factory,
    )

    assert {scope.identity for scope in resolved_scopes} == {f"project:{project_id}"}
    assert {scope.project_root for scope in resolved_scopes} == {Path("/tmp/wiki").resolve()}
    assert sorted(job.name for job in cron_storage.list_jobs(project_id=project_id)) == [
        f"gobby:wiki-{command}:project:{project_id}" for command in WIKI_JOB_COMMANDS
    ]


@pytest.mark.asyncio
async def test_wiki_cron_registration_leaves_bare_uuid_rows_untouched(
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
    legacy_research = cron_storage.create_job(
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

    untouched_refresh = cron_storage.get_job(legacy_refresh.id)
    assert untouched_refresh is not None
    assert untouched_refresh.name == f"gobby:wiki-refresh:{project_id}"
    assert untouched_refresh.enabled is True
    assert cron_storage.get_job(canonical_refresh.id) is not None
    assert cron_storage.get_job(legacy_research.id) is not None
    assert cron_storage.get_job_by_name(f"gobby:wiki-research:project:{project_id}") is None


@pytest.mark.asyncio
async def test_disabled_non_system_row_takeover_preserves_enabled_and_marks_system(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    legacy = cron_storage.create_job(
        project_id=project_id,
        name="gobby:wiki-refresh:project:alpha",
        description="operator refresh",
        schedule_type="interval",
        interval_seconds=600,
        action_type="shell",
        action_config={"command": "true"},
        enabled=False,
        is_system=False,
    )
    assert legacy.next_run_at is None
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

    assert created == len(WIKI_JOB_COMMANDS)
    assert repeated == len(WIKI_JOB_COMMANDS)
    taken_over = cron_storage.get_job(legacy.id)
    assert taken_over is not None
    assert taken_over.is_system is True
    # The operator's disabled toggle survives the takeover and every restart.
    assert taken_over.enabled is False
    assert taken_over.next_run_at is None
    assert taken_over.action_type == "handler"
    assert taken_over.action_config["handler"] == "wiki:refresh:project:alpha"
    assert taken_over.interval_seconds == 3600


@pytest.mark.asyncio
async def test_enabled_non_system_row_takeover_recomputes_next_run(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    legacy = cron_storage.create_job(
        project_id=project_id,
        name="gobby:wiki-recap:project:alpha",
        description="operator recap",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="shell",
        action_config={"command": "true"},
        enabled=True,
        is_system=False,
    )

    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=project_id,
        scopes=["project:alpha"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    taken_over = cron_storage.get_job(legacy.id)
    assert taken_over is not None
    assert taken_over.is_system is True
    assert taken_over.enabled is True
    assert taken_over.schedule_type == "cron"
    assert taken_over.cron_expr == WIKI_RECAP_SCHEDULE_CRON
    assert taken_over.next_run_at is not None
    recomputed = taken_over.next_run_at.astimezone(ZoneInfo(taken_over.timezone))
    assert (recomputed.hour, recomputed.minute) == (0, 10)


@pytest.mark.asyncio
async def test_non_system_bare_scope_row_does_not_abort_registration(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    operator_row = cron_storage.create_job(
        project_id=project_id,
        name=f"gobby:wiki-refresh:{project_id}",
        description="operator refresh on the legacy bare scope",
        schedule_type="interval",
        interval_seconds=3600,
        action_type="shell",
        action_config={"command": "true"},
        enabled=True,
        is_system=False,
    )
    executor = RecordingExecutor(handlers={})

    created = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    assert created == len(WIKI_JOB_COMMANDS)
    assert set(executor.handlers) == {
        f"wiki:{command}:project:{project_id}" for command in WIKI_JOB_COMMANDS
    }
    untouched = cron_storage.get_job(operator_row.id)
    assert untouched is not None
    assert untouched.name == f"gobby:wiki-refresh:{project_id}"
    assert untouched.is_system is False
    assert untouched.enabled is True


@pytest.mark.asyncio
async def test_startup_registers_handlers_for_other_projects_enabled_rows(
    cron_storage: CronJobStorage,
    project_id: str,
    temp_db: Any,
) -> None:
    # A previous startup in another project created its enabled system rows.
    other_project = LocalProjectManager(temp_db).create(name="wiki-b", repo_path="/tmp/wiki-b").id
    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=other_project,
        scopes=[f"project:{other_project}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    executor = RecordingExecutor(handlers={})

    registered = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    # Configured-scope handlers plus the same set swept from the other project's rows.
    assert registered == 2 * len(WIKI_JOB_COMMANDS)
    assert set(executor.handlers) == {
        f"wiki:{command}:project:{project_id}" for command in WIKI_JOB_COMMANDS
    } | {f"wiki:{command}:project:{other_project}" for command in WIKI_JOB_COMMANDS}
    other_refresh = cron_storage.get_job_by_name(f"gobby:wiki-refresh:project:{other_project}")
    assert other_refresh is not None
    assert other_refresh.enabled is True
    assert other_refresh.next_run_at is not None


@pytest.mark.asyncio
async def test_startup_parks_rows_for_unresolvable_scope(
    cron_storage: CronJobStorage,
    project_id: str,
    temp_db: Any,
) -> None:
    # Rows whose scope names a project that is not in the projects table.
    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=project_id,
        scopes=["project:ghost-project"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    executor = RecordingExecutor(handlers={})

    registered = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        db=temp_db,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    assert registered == len(WIKI_JOB_COMMANDS)
    assert set(executor.handlers) == {
        f"wiki:{command}:project:{project_id}" for command in WIKI_JOB_COMMANDS
    }
    for command in WIKI_JOB_COMMANDS:
        job = cron_storage.get_job_by_name(f"gobby:wiki-{command}:project:ghost-project")
        assert job is not None
        # Parked, not disabled: the operator toggle is preserved while the
        # row stops coming due (and stops failing with "No handler registered").
        assert job.enabled is True
        assert job.next_run_at is None


@pytest.mark.asyncio
async def test_startup_disables_rows_for_soft_deleted_project(
    cron_storage: CronJobStorage,
    project_id: str,
    temp_db: Any,
) -> None:
    # A previous startup created enabled rows for a project that was later
    # soft-deleted (repo merged away, #18330): parking would re-warn on every
    # startup forever, so the rows are disabled once with an audit trail.
    manager = LocalProjectManager(temp_db)
    dead_project = manager.create(name="wiki-dead", repo_path="/tmp/wiki-dead").id
    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=dead_project,
        scopes=[f"project:{dead_project}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    assert manager.soft_delete(dead_project) is True
    executor = RecordingExecutor(handlers={})

    registered = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        db=temp_db,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    assert registered == len(WIKI_JOB_COMMANDS)
    for command in WIKI_JOB_COMMANDS:
        job = cron_storage.get_job_by_name(f"gobby:wiki-{command}:project:{dead_project}")
        assert job is not None
        # Disabled, not parked: the scope can never resolve again.
        assert job.enabled is False
        assert job.next_run_at is None
        assert f"wiki:{command}:project:{dead_project}" not in executor.handlers

    # The next sweep sees no enabled rows for the dead scope: nothing to
    # disable again, nothing to warn about, and the live scope re-registers.
    rerun = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=project_id,
        db=temp_db,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    assert rerun == len(WIKI_JOB_COMMANDS)
    for command in WIKI_JOB_COMMANDS:
        job = cron_storage.get_job_by_name(f"gobby:wiki-{command}:project:{dead_project}")
        assert job is not None
        assert job.enabled is False


@pytest.mark.asyncio
async def test_startup_keeps_parking_live_project_with_missing_root(
    cron_storage: CronJobStorage,
    project_id: str,
    temp_db: Any,
    tmp_path: Path,
) -> None:
    # A registered, live project whose repo path is missing (unmounted
    # volume) must keep today's parking — never lose its schedules (#18330).
    vanished_root = tmp_path / "unmounted" / "wiki-away"
    away_project = (
        LocalProjectManager(temp_db).create(name="wiki-away", repo_path=str(vanished_root)).id
    )
    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=away_project,
        scopes=[f"project:{away_project}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    executor = RecordingExecutor(handlers={})

    # gateway_factory=None exercises the root-existence check used at startup.
    registered = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        db=temp_db,
        scopes=[],
        gateway_factory=None,
    )

    assert registered == 0
    for command in WIKI_JOB_COMMANDS:
        job = cron_storage.get_job_by_name(f"gobby:wiki-{command}:project:{away_project}")
        assert job is not None
        # Parked, not disabled: the scope may resolve again on a later sweep.
        assert job.enabled is True
        assert job.next_run_at is None


@pytest.mark.asyncio
async def test_sweep_wakes_parked_rows_when_scope_resolves(
    cron_storage: CronJobStorage,
    project_id: str,
    temp_db: Any,
) -> None:
    other_project = LocalProjectManager(temp_db).create(name="wiki-b", repo_path="/tmp/wiki-b").id
    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=other_project,
        scopes=[f"project:{other_project}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    for command in WIKI_JOB_COMMANDS:
        job = cron_storage.get_job_by_name(f"gobby:wiki-{command}:project:{other_project}")
        assert job is not None
        cron_storage.park_system_job(job.id)
    executor = RecordingExecutor(handlers={})

    registered = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        db=temp_db,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    assert registered == 2 * len(WIKI_JOB_COMMANDS)
    for command in WIKI_JOB_COMMANDS:
        job = cron_storage.get_job_by_name(f"gobby:wiki-{command}:project:{other_project}")
        assert job is not None
        assert job.enabled is True
        assert job.next_run_at is not None
        assert f"wiki:{command}:project:{other_project}" in executor.handlers


@pytest.mark.asyncio
async def test_registration_wakes_parked_row_for_configured_scope(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=project_id,
        scopes=["project:alpha"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    parked = cron_storage.get_job_by_name("gobby:wiki-refresh:project:alpha")
    assert parked is not None
    cron_storage.park_system_job(parked.id)

    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=project_id,
        scopes=["project:alpha"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    woken = cron_storage.get_job(parked.id)
    assert woken is not None
    assert woken.enabled is True
    assert woken.next_run_at is not None


@pytest.mark.asyncio
async def test_parked_other_project_scope_keeps_handlers_registered(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=project_id,
        scopes=["project:idle-scope"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    for command in WIKI_JOB_COMMANDS:
        job = cron_storage.get_job_by_name(f"gobby:wiki-{command}:project:idle-scope")
        assert job is not None
        cron_storage.park_system_job(job.id)
    executor = RecordingExecutor(handlers={})

    registered = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id=project_id,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    # Parked system rows remain enabled but have no next run, so their handlers
    # stay registered even though the scheduler will not claim them.
    assert registered == 2 * len(WIKI_JOB_COMMANDS)
    assert set(executor.handlers) == {
        f"wiki:{command}:{scope}"
        for command in WIKI_JOB_COMMANDS
        for scope in ("project:idle-scope", f"project:{project_id}")
    }


@pytest.mark.asyncio
async def test_registration_without_startup_project_sweeps_enabled_rows(
    cron_storage: CronJobStorage,
    project_id: str,
) -> None:
    await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=RecordingExecutor(handlers={}),
        project_id=project_id,
        scopes=[f"project:{project_id}"],
        gateway_factory=lambda _scope: RecordingGateway(),
    )
    executor = RecordingExecutor(handlers={})

    # Daemon started outside any project: no configured scopes, sweep only.
    registered = await register_wiki_cron_jobs(
        cron_storage=cron_storage,
        cron_executor=executor,
        project_id="",
        scopes=[],
        gateway_factory=lambda _scope: RecordingGateway(),
    )

    assert registered == len(WIKI_JOB_COMMANDS)
    assert set(executor.handlers) == {
        f"wiki:{command}:project:{project_id}" for command in WIKI_JOB_COMMANDS
    }


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

    assert gateway.calls == [
        (
            "upkeep",
            {
                "dry_run": False,
                "ai": "auto",
                "max_pages": 10,
                "time_budget_seconds": 1320,
            },
        )
    ]
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

    assert created == 2 * len(WIKI_JOB_COMMANDS)
    job_names = sorted(job.name for job in cron_storage.list_jobs(project_id=project_id))
    assert job_names == sorted(
        [f"gobby:wiki-{command}:project:alpha" for command in WIKI_JOB_COMMANDS]
        + [f"gobby:wiki-{command}:topic:sessions" for command in WIKI_JOB_COMMANDS]
    )

    for scope in ("project:alpha", "topic:sessions"):
        librarian_job = next(
            job
            for job in cron_storage.list_jobs(project_id=project_id)
            if job.name == f"gobby:wiki-librarian:{scope}"
        )
        output = json.loads(await executor.handlers[f"wiki:librarian:{scope}"](librarian_job))
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
