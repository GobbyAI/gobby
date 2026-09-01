"""Hard-delete project service and retention cron orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypedDict

from gobby.code_index.maintenance_launch import MaintenanceLaunchFactory, open_launch_async
from gobby.scheduler.executor import CronHandler
from gobby.storage.cron_models import CronJob
from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.projects import PERSONAL_PROJECT_ID

PROJECT_PURGE_JOB_NAME = "gobby:project-purge"
PROJECT_PURGE_HANDLER_NAME = "projects:purge-expired"
PROJECT_PURGE_INTERVAL_SECONDS = 24 * 60 * 60
PROJECT_PURGE_RETENTION_HOURS = 24
PROJECT_PURGE_DESCRIPTION = "Purge project state retained beyond the soft-delete window"
PROJECT_PURGE_ID_LIMIT = 10
PROJECT_PURGE_CONCURRENCY = 4


class ProjectPurgeError(RuntimeError):
    """A retryable purge phase failed."""


class ProjectPurgeVectorStoreUnavailable(ProjectPurgeError):
    """Qdrant is configured but no runtime vector store could be resolved."""


_LEDGER_PURGE_BATCH_SIZE = 100


@dataclass(frozen=True)
class PurgeOutcome:
    """Result of one project purge attempt."""

    project_id: str
    success: bool
    status: Literal["purged", "failed", "protected", "not_found"]
    message: str

    @classmethod
    def purged(cls, project_id: str) -> PurgeOutcome:
        return cls(project_id, True, "purged", "Project purge completed")

    @classmethod
    def failed(cls, project_id: str, message: str) -> PurgeOutcome:
        return cls(project_id, False, "failed", message)

    @classmethod
    def protected(cls, project_id: str, message: str) -> PurgeOutcome:
        return cls(project_id, False, "protected", message)

    @classmethod
    def not_found(cls, project_id: str) -> PurgeOutcome:
        return cls(project_id, False, "not_found", "Project not found")


class PurgeBatchResult(TypedDict):
    success: bool
    status: Literal["completed", "failed"]
    message: str
    purged: list[str]
    purged_count: int
    failed: list[str]
    failed_count: int
    skipped_protected: list[str]
    skipped_protected_count: int


class ProjectRow(Protocol):
    id: str
    name: str
    deleted_at: datetime | None


class ProjectStorage(Protocol):
    def get(self, project_id: str) -> ProjectRow | None: ...
    def is_protected(self, project: Any) -> bool: ...
    def soft_delete(self, project_id: str) -> bool: ...
    def list_purge_candidates(self, cutoff: datetime) -> Sequence[ProjectRow]: ...


class CronStorage(Protocol):
    def disable_project_jobs(self, project_id: str) -> Sequence[Any]: ...
    def list_active_runs(self) -> Sequence[Any]: ...
    def delete_project_jobs(self, job_ids: list[str]) -> int: ...


class ExclusiveFence(Protocol):
    def exclusive(
        self, project_id: str, *, timeout: float
    ) -> AbstractAsyncContextManager[None]: ...


class GwikiDrainBarrier(Protocol):
    def drain(self, project_id: str, *, timeout: float) -> AbstractAsyncContextManager[None]: ...


# Rows outside the purged project that still reference its tasks or sessions
# through NO ACTION / RESTRICT foreign keys (tasks re-parented across projects,
# sessions and agent runs spawned across projects, audit rows). Detach or drop
# them first; a bare DELETE of tasks/sessions would otherwise violate the FK.
_PROJECT_TASKS = "SELECT id FROM tasks WHERE project_id = %s"
_PROJECT_SESSIONS = "SELECT id FROM sessions WHERE project_id = %s"
# tasks_require_validation_criteria is NOT VALID: legacy rows without criteria
# reject any UPDATE, so a detach that must rewrite such a row backfills a
# placeholder rather than failing the purge.
_LEGACY_CRITERIA_BACKFILL = (
    "validation_criteria = CASE"
    " WHEN task_type = 'epic' OR NULLIF(btrim(validation_criteria), '') IS NOT NULL"
    " THEN validation_criteria"
    " ELSE 'Legacy task: validation criteria were not recorded before they became required.'"
    " END"
)
_FOREIGN_REFERENCE_DETACH_STATEMENTS: tuple[tuple[str, int], ...] = (
    (
        f"UPDATE tasks SET parent_task_id = NULL, {_LEGACY_CRITERIA_BACKFILL}"
        f" WHERE parent_task_id IN ({_PROJECT_TASKS}) AND project_id <> %s",
        2,
    ),
    (
        f"UPDATE tasks SET created_in_session_id = NULL, {_LEGACY_CRITERIA_BACKFILL}"
        f" WHERE created_in_session_id IN ({_PROJECT_SESSIONS}) AND project_id <> %s",
        2,
    ),
    (
        f"UPDATE tasks SET closed_in_session_id = NULL, {_LEGACY_CRITERIA_BACKFILL}"
        f" WHERE closed_in_session_id IN ({_PROJECT_SESSIONS}) AND project_id <> %s",
        2,
    ),
    (
        f"UPDATE tasks SET claimed_by_session_id = NULL, {_LEGACY_CRITERIA_BACKFILL}"
        f" WHERE claimed_by_session_id IN ({_PROJECT_SESSIONS}) AND project_id <> %s",
        2,
    ),
    (
        "UPDATE sessions SET parent_session_id = NULL"
        f" WHERE parent_session_id IN ({_PROJECT_SESSIONS}) AND project_id <> %s",
        2,
    ),
    (f"DELETE FROM workflow_audit_log WHERE session_id IN ({_PROJECT_SESSIONS})", 1),
    (f"DELETE FROM agent_runs WHERE parent_session_id IN ({_PROJECT_SESSIONS})", 1),
    (
        "UPDATE agent_runs SET child_session_id = NULL"
        f" WHERE child_session_id IN ({_PROJECT_SESSIONS})",
        1,
    ),
    (
        "UPDATE agent_runs SET claimed_session_id = NULL"
        f" WHERE claimed_session_id IN ({_PROJECT_SESSIONS})",
        1,
    ),
)


class WikiGateway(Protocol):
    async def purge_project_scope(
        self, project_id: str, *, timeout: float, env: Mapping[str, str] | None = None
    ) -> Any: ...


class CodeGateway(Protocol):
    async def invalidate_project_by_id(
        self, project_id: str, *, timeout: float, env: Mapping[str, str] | None = None
    ) -> Any: ...


class VectorCleaner(Protocol):
    async def clear_project(self, project_id: str, memory_ids: list[str]) -> None: ...


class GraphCleaner(Protocol):
    async def clear_project_graph_strict(self, project_id: str) -> dict[str, int]: ...


class NoopProjectVectorCleaner:
    """No-op cleanup used only when vector storage is not configured."""

    async def clear_project(self, project_id: str, memory_ids: list[str]) -> None:
        return None


class NoopProjectGraphCleaner:
    """No-op cleanup used only when graph storage is not configured."""

    async def clear_project_graph_strict(self, project_id: str) -> dict[str, int]:
        return {"memories_deleted": 0, "entities_deleted": 0}


class ProjectPurgeService:
    """Own the ordered, retry-safe purge of one soft-deleted project."""

    def __init__(
        self,
        *,
        db: Any,
        projects: ProjectStorage,
        cron: CronStorage,
        fence: ExclusiveFence,
        gwiki_barrier: GwikiDrainBarrier,
        wiki_gateway: WikiGateway,
        code_gateway: CodeGateway,
        vector_cleaner: Callable[[], VectorCleaner],
        graph_cleaner: Callable[[], GraphCleaner],
        launch_factory: Callable[[], MaintenanceLaunchFactory | None] | None = None,
        drain_timeout: float = 120.0,
        command_timeout: float = 120.0,
    ) -> None:
        self.db = db
        self.projects = projects
        self.cron = cron
        self.fence = fence
        self.gwiki_barrier = gwiki_barrier
        self.wiki_gateway = wiki_gateway
        self.code_gateway = code_gateway
        self.vector_cleaner = vector_cleaner
        self.graph_cleaner = graph_cleaner
        self.drain_timeout = drain_timeout
        self.command_timeout = command_timeout
        self.launch_factory = launch_factory

    @asynccontextmanager
    async def _maintenance_env(self, project_id: str) -> AsyncIterator[dict[str, str] | None]:
        """Grant gwiki/gcode a maintenance launch: the project is soft-deleted, so an
        interactive grant is refused and its checkouts are already released."""
        factory = self.launch_factory() if self.launch_factory is not None else None
        if factory is None:
            yield None
            return
        async with open_launch_async(
            factory, project_id, timeout_seconds=self.command_timeout
        ) as launch:
            yield launch.env

    async def purge_project(self, project_id: str) -> PurgeOutcome:
        try:
            return await self._purge_project(project_id)
        except Exception as exc:
            return PurgeOutcome.failed(project_id, str(exc))

    async def _purge_project(self, project_id: str) -> PurgeOutcome:
        project = await asyncio.to_thread(self.projects.get, project_id)
        if project is None:
            return PurgeOutcome.not_found(project_id)
        if self.projects.is_protected(project):
            return PurgeOutcome.protected(project_id, f"Project '{project.name}' is protected")

        if project.deleted_at is None:
            deleted = await asyncio.to_thread(self.projects.soft_delete, project_id)
            if not deleted:
                return PurgeOutcome.failed(project_id, "Failed to soft-delete project")

        # Resolve runtime-bound dependencies before deleting cron jobs. A
        # resolver failure leaves the retry mechanism intact.
        vector_cleaner = self.vector_cleaner()
        graph_cleaner = self.graph_cleaner()
        jobs = await asyncio.to_thread(self.cron.disable_project_jobs, project_id)
        job_ids = [str(job.id) for job in jobs]
        await self._drain_cron_runs(job_ids)
        await asyncio.to_thread(self.cron.delete_project_jobs, job_ids)

        async with self.fence.exclusive(project_id, timeout=self.drain_timeout):
            async with self.gwiki_barrier.drain(project_id, timeout=self.drain_timeout):
                pass
            memory_ids = await asyncio.to_thread(self._memory_ids, project_id)
            await self._purge_wiki(project_id)
            await self._invalidate_code(project_id)
            await vector_cleaner.clear_project(project_id, memory_ids)
            await graph_cleaner.clear_project_graph_strict(project_id)
            await asyncio.to_thread(self._delete_hub_rows, project_id)
        return PurgeOutcome.purged(project_id)

    async def _drain_cron_runs(self, job_ids: list[str]) -> None:
        if not job_ids:
            return
        deadline = time.monotonic() + self.drain_timeout
        job_id_set = set(job_ids)
        while True:
            runs = await asyncio.to_thread(self.cron.list_active_runs)
            if not any(str(run.cron_job_id) in job_id_set for run in runs):
                return
            if time.monotonic() >= deadline:
                raise ProjectPurgeError("Timed out draining project cron runs")
            await asyncio.sleep(min(0.05, max(deadline - time.monotonic(), 0)))

    async def _purge_wiki(self, project_id: str) -> None:
        async with self._maintenance_env(project_id) as env:
            result = await self.wiki_gateway.purge_project_scope(
                project_id, timeout=self.command_timeout, env=env
            )
        if not bool(result.success):
            raise ProjectPurgeError(_command_failure("gwiki purge", result))

    async def _invalidate_code(self, project_id: str) -> None:
        async with self._maintenance_env(project_id) as env:
            result = await self.code_gateway.invalidate_project_by_id(
                project_id, timeout=self.command_timeout, env=env
            )
        if not bool(result.success):
            raise ProjectPurgeError(_command_failure("gcode invalidate", result))

    def _memory_ids(self, project_id: str) -> list[str]:
        rows = self.db.fetchall("SELECT id FROM memories WHERE project_id = %s", (project_id,))
        return [str(row["id"]) for row in rows]

    def _delete_hub_rows(self, project_id: str) -> None:
        generation_state = EmbeddingGenerationState(self.db)
        tombstone_batches = (
            (
                "memory",
                "SELECT id AS row_id, id::text AS source_id FROM memories "
                "WHERE project_id = %s LIMIT %s",
                "DELETE FROM memories WHERE id = ANY(%s)",
            ),
            (
                "github_issue",
                "SELECT id AS row_id, project_id::text || ':' || repo || ':' || "
                "issue_number::text AS source_id FROM gh_issues_triaged "
                "WHERE project_id = %s LIMIT %s",
                "DELETE FROM gh_issues_triaged WHERE id = ANY(%s)",
            ),
            (
                "tool",
                "SELECT tools.id AS row_id, tools.id::text AS source_id FROM tools "
                "JOIN mcp_servers ON mcp_servers.id = tools.mcp_server_id "
                "WHERE mcp_servers.project_id = %s LIMIT %s",
                "DELETE FROM tools WHERE id = ANY(%s)",
            ),
        )
        # Each transaction holds the shared projection-ledger lock for at most
        # one bounded batch, allowing an exclusive switch watermark to proceed.
        for source_kind, select_sql, delete_sql in tombstone_batches:
            while True:
                with self.db.transaction() as transaction:
                    rows = transaction.execute(
                        select_sql, (project_id, _LEDGER_PURGE_BATCH_SIZE)
                    ).fetchall()
                    if not rows:
                        break
                    for row in rows:
                        generation_state.append_change(
                            source_kind,
                            str(row["source_id"]),
                            is_tombstone=True,
                            transaction=transaction,
                        )
                    transaction.execute(delete_sql, ([row["row_id"] for row in rows],))

        with self.db.transaction() as transaction:
            for statement, arity in _FOREIGN_REFERENCE_DETACH_STATEMENTS:
                transaction.execute(statement, (project_id,) * arity)
            for table in ("tasks", "plans", "sessions"):
                transaction.execute(
                    f"DELETE FROM {table} WHERE project_id = %s",  # nosec B608
                    (project_id,),
                )
            transaction.execute("DELETE FROM projects WHERE id = %s", (project_id,))


def create_project_purge_handler(service: ProjectPurgeService) -> CronHandler:
    """Create a failure-isolated daily retention handler."""

    async def _handler(_job: CronJob) -> PurgeBatchResult:
        cutoff = datetime.now(UTC) - timedelta(hours=PROJECT_PURGE_RETENTION_HOURS)
        projects = await asyncio.to_thread(service.projects.list_purge_candidates, cutoff)
        semaphore = asyncio.Semaphore(PROJECT_PURGE_CONCURRENCY)

        async def purge_candidate(project_id: str) -> tuple[str, str]:
            async with semaphore:
                try:
                    result = await service.purge_project(project_id)
                except Exception:
                    return "failed", project_id
            if result.success:
                return "purged", project_id
            if result.status == "protected":
                return "protected", project_id
            return "failed", project_id

        results = await asyncio.gather(*(purge_candidate(project.id) for project in projects))
        purged: list[str] = []
        failed: list[str] = []
        protected: list[str] = []
        for status, project_id in results:
            if status == "purged":
                purged.append(project_id)
            elif status == "protected":
                protected.append(project_id)
            else:
                failed.append(project_id)
        success = not failed
        return {
            "success": success,
            "status": "completed" if success else "failed",
            "message": (
                f"Purged {len(purged)} project(s); {len(failed)} failed; {len(protected)} protected"
            ),
            "purged": purged[:PROJECT_PURGE_ID_LIMIT],
            "purged_count": len(purged),
            "failed": failed[:PROJECT_PURGE_ID_LIMIT],
            "failed_count": len(failed),
            "skipped_protected": protected[:PROJECT_PURGE_ID_LIMIT],
            "skipped_protected_count": len(protected),
        }

    return _handler


def register_project_purge_cron(
    cron_storage: Any,
    cron_executor: Any,
    service: ProjectPurgeService,
    *,
    project_id: str | None = None,
) -> None:
    """Register and name-reconcile the daily purge system job."""
    cron_executor.register_handler(
        PROJECT_PURGE_HANDLER_NAME, create_project_purge_handler(service)
    )
    action_config = {
        "handler": PROJECT_PURGE_HANDLER_NAME,
        "purpose": PROJECT_PURGE_DESCRIPTION,
    }
    existing = cron_storage.get_job_by_name(PROJECT_PURGE_JOB_NAME)
    if existing is None:
        cron_storage.create_job(
            project_id=project_id or PERSONAL_PROJECT_ID,
            name=PROJECT_PURGE_JOB_NAME,
            description=PROJECT_PURGE_DESCRIPTION,
            schedule_type="interval",
            interval_seconds=PROJECT_PURGE_INTERVAL_SECONDS,
            action_type="handler",
            action_config=action_config,
            enabled=True,
            is_system=True,
        )
        return
    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)
    reconciled = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=action_config,
        description=PROJECT_PURGE_DESCRIPTION,
        schedule_type="interval",
        interval_seconds=PROJECT_PURGE_INTERVAL_SECONDS,
    )
    if reconciled is not None and reconciled.enabled and reconciled.next_run_at is None:
        cron_storage.wake_system_job(reconciled.id)


def _command_failure(command: str, result: Any) -> str:
    detail = str(getattr(result, "stderr", "")).strip()
    return detail or f"{command} failed"
