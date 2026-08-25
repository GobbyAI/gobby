"""gcode prune automation for code-index projection drift."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypedDict
from uuid import uuid4

from gobby.code_index.eligibility import resolve_indexed_project
from gobby.code_index.gcode_gateway import GcodeCommandError, GcodeCommandResult
from gobby.code_index.maintenance import _reconcile_stale_selector
from gobby.code_index.maintenance_launch import open_launch_async
from gobby.code_index.maintenance_log import log_gcode_maintenance_event
from gobby.scheduler.executor import CronHandler
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import PERSONAL_PROJECT_ID

if TYPE_CHECKING:
    from gobby.code_index.context import CodeIndexContext

logger = logging.getLogger(__name__)

CODE_INDEX_PRUNE_JOB_NAME = "gobby:code-index-prune"
CODE_INDEX_PRUNE_HANDLER = "code-index:prune"
CODE_INDEX_PRUNE_INTERVAL_SECONDS = 3600
CODE_INDEX_PRUNE_TIMEOUT_SECONDS = 120
CODE_INDEX_PRUNE_LOCK_TIMEOUT_SECONDS = 5.0
CODE_INDEX_PRUNE_DESCRIPTION = (
    "Prune stale code-index projections and perform orphan Qdrant collection cleanup"
)
_NO_STALE_PROJECTS = "No stale projects found."
_DEFAULT_MAINTENANCE_LOG_FILE = "~/.gobby/logs/code-index-maintenance.log"
_OPERATOR_COMPLETED_SUFFIXES = frozenset({"pruned", "deferred_pending_sync", "reconciled"})
_OPERATOR_SKIPPED_SUFFIXES = frozenset({"skipped_locked", "skipped_missing_root"})
_OPERATOR_HUB_DELETE_SUFFIXES = frozenset({"pruned"})


@asynccontextmanager
async def _held_project_lock(
    lock: asyncio.Lock, *, force: bool, timeout: float
) -> AsyncIterator[bool]:
    if lock.locked() and not force:
        yield False
        return
    acquired = False
    if force:
        try:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
        except TimeoutError:
            yield False
            return
        acquired = True
    try:
        if not acquired:
            async with lock:
                yield True
                return
        yield True
    finally:
        if acquired:
            lock.release()


def _is_noop_shutdown_prune(exc: Exception) -> bool:
    return (
        isinstance(exc, GcodeCommandError)
        and exc.returncode == -signal.SIGTERM
        and (_NO_STALE_PROJECTS in exc.stdout or _NO_STALE_PROJECTS in exc.stderr)
    )


def _is_noop_shutdown_result(result: GcodeCommandResult) -> bool:
    return result.returncode == -signal.SIGTERM and (
        _NO_STALE_PROJECTS in result.stdout or _NO_STALE_PROJECTS in result.stderr
    )


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


class CodeIndexPruneResult(TypedDict):
    success: bool
    status: Literal["completed", "skipped", "failed", "timed_out", "unavailable"]
    run_id: str | None
    message: str
    stdout: str
    stderr: str
    retried_projects: int


class OperatorPruneItem(TypedDict, total=False):
    project_id: str
    reason: str


class OperatorPruneOutcome(TypedDict):
    completed: list[str]
    failed: list[OperatorPruneItem]
    skipped: list[OperatorPruneItem]


class CodeIndexPruner:
    """Coordinates startup and cron gcode prune runs."""

    def __init__(self, context: CodeIndexContext, *, max_concurrency: int = 1) -> None:
        self._context = context
        self._global_semaphore = asyncio.Semaphore(max(1, max_concurrency))
        self._global_lock = asyncio.Lock()
        self._project_locks: dict[str, asyncio.Lock] = {}

    async def prune_dirty_projects(self, *, limit: int = 100) -> str:
        outcomes: list[str] = []
        processed_project_ids: set[str] = set()
        deferred_dirty_projects: list[Any] = []
        after: tuple[Any, Any, str] | None = None
        remaining = max(0, limit)
        while remaining > 0:
            dirty_projects = await self._context.run_db(
                self._context.storage.list_prune_dirty_projects,
                min(remaining, 1000),
                after,
            )
            if not dirty_projects:
                break

            after = _dirty_prune_cursor(dirty_projects[-1])
            pending = [
                dirty for dirty in dirty_projects if dirty.project_id not in processed_project_ids
            ]
            if not pending:
                break

            for dirty in pending:
                processed_project_ids.add(dirty.project_id)
                outcome = await self.prune_project(
                    project_id=dirty.project_id,
                    root_path=dirty.root_path,
                    dirty=True,
                    reason=dirty.reason,
                )
                outcomes.append(outcome)
                remaining -= 1
                if outcome.endswith(":deferred_pending_sync"):
                    deferred_dirty_projects.append(dirty)
                if remaining <= 0:
                    break
        for dirty in deferred_dirty_projects:
            await self._context.run_db(
                self._context.storage.mark_prune_dirty,
                dirty.project_id,
                dirty.root_path,
                dirty.reason,
            )
        if not outcomes:
            return "Code index prune skipped: dirty=0"
        return "Code index prune completed: " + ", ".join(outcomes)

    async def run_operator_global_prune(
        self,
        *,
        force: bool = False,
        retention_days: int | None = None,
    ) -> OperatorPruneOutcome:
        """Snapshot indexed projects, reconcile projections, then delete stale hub rows."""
        async with self._global_lock:
            return await self._run_operator_global_prune_locked(
                force=force,
                retention_days=retention_days,
            )

    async def _run_operator_global_prune_locked(
        self,
        *,
        force: bool,
        retention_days: int | None,
    ) -> OperatorPruneOutcome:
        snapshot = list(await self._context.run_db(self._context.storage.list_indexed_projects))
        completed: list[str] = []
        failed: list[OperatorPruneItem] = []
        skipped: list[OperatorPruneItem] = []

        async def _handle(project: Any) -> None:
            async with self._global_semaphore:
                project_id = str(getattr(project, "id", "") or "")
                root_path = str(getattr(project, "root_path", "") or "")
                if not project_id:
                    skipped.append({"project_id": "", "reason": "missing_id"})
                    return
                if not root_path:
                    skipped.append({"project_id": project_id, "reason": "missing_root"})
                    return
                root = Path(root_path).expanduser()
                outcome = await self.prune_project(
                    project_id=project_id,
                    root_path=str(root),
                    dirty=True,
                    reason="operator_global_prune",
                    force=force,
                    retention_days=retention_days,
                )
                suffix = outcome.rsplit(":", 1)[-1]
                if suffix in _OPERATOR_COMPLETED_SUFFIXES:
                    if suffix in _OPERATOR_HUB_DELETE_SUFFIXES and not await asyncio.to_thread(
                        root.exists
                    ):
                        await self._context.run_db(
                            self._context.storage.delete_project_index,
                            project_id,
                        )
                    completed.append(project_id)
                    return
                if suffix in _OPERATOR_SKIPPED_SUFFIXES:
                    skipped.append({"project_id": project_id, "reason": suffix})
                    return
                await self._context.run_db(
                    self._context.storage.mark_prune_dirty,
                    project_id,
                    root_path,
                    "operator_prune_failed",
                )
                failed.append({"project_id": project_id, "reason": outcome})

        await asyncio.gather(*(_handle(project) for project in snapshot))
        return {
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
        }

    async def prune_all_projects(self) -> CodeIndexPruneResult:
        """Run global prune in-process under the same lock the HTTP route uses."""
        if self._global_lock.locked():
            return {
                "success": True,
                "status": "skipped",
                "run_id": None,
                "message": "Code index prune skipped: global_locked",
                "stdout": "",
                "stderr": "",
                "retried_projects": 0,
            }

        async with self._global_lock:
            run_id = uuid4().hex
            started_at = datetime.now(UTC).isoformat()
            started = perf_counter()
            outcome = await self._run_operator_global_prune_locked(
                force=True,
                retention_days=self._context.config.content_retention_days,
            )
            status: Literal["completed", "failed"] = "failed" if outcome["failed"] else "completed"
            stdout = json.dumps(outcome, sort_keys=True)
            log_gcode_maintenance_event(
                log_file=_maintenance_log_file(self._context),
                event="global_prune",
                run_id=run_id,
                project_id=None,
                root_path=None,
                result=GcodeCommandResult(
                    command=("in-process", "global_prune"),
                    returncode=0 if status == "completed" else 1,
                    stdout=stdout,
                    stderr="",
                    started_at=started_at,
                    completed_at=datetime.now(UTC).isoformat(),
                    duration_seconds=perf_counter() - started,
                    timeout_seconds=None,
                ),
                status=status,
            )
            if status == "completed":
                await self._clear_dirty_projects()
            return {
                "success": status == "completed",
                "status": status,
                "run_id": run_id,
                "message": (
                    f"Code index prune completed: run_id={run_id} global:{status} "
                    f"failed={len(outcome['failed'])} skipped={len(outcome['skipped'])}"
                ),
                "stdout": stdout,
                "stderr": "",
                "retried_projects": 0,
            }

    async def prune_project(
        self,
        *,
        project_id: str,
        root_path: str,
        dirty: bool,
        reason: str,
        run_id: str | None = None,
        force: bool = False,
        retention_days: int | None = None,
    ) -> str:
        lock = self._project_locks.setdefault(project_id, asyncio.Lock())
        resolved_retention = (
            retention_days
            if retention_days is not None
            else self._context.config.content_retention_days
        )

        async with _held_project_lock(
            lock, force=force, timeout=CODE_INDEX_PRUNE_LOCK_TIMEOUT_SECONDS
        ) as acquired:
            if not acquired:
                return f"{project_id}:skipped_locked"
            pending = await self._context.run_db(
                self._context.storage.get_pending_sync_files,
                project_id,
                1,
                vectors=True,
                graph=True,
            )
            if pending:
                return f"{project_id}:deferred_pending_sync"

            lookup = getattr(self._context.storage, "get_registry_project", None)
            if callable(lookup):
                raw = await self._context.run_db(lookup, project_id)
                exists, deleted = bool(raw[0]), bool(raw[1])
                decision = resolve_indexed_project(
                    project_id,
                    root_path,
                    project_exists=exists,
                    project_deleted=deleted,
                )
                if decision.kind == "overlay":
                    # Live worktree/clone overlay: owned by overlay-claim
                    # launches, not the registry-keyed prune pass (#20889).
                    return f"{project_id}:skipped_overlay"
                if decision.kind != "active":
                    outcome = await _reconcile_stale_selector(
                        self._context, project_id, decision.kind
                    )
                    return f"{project_id}:{outcome}"

            gateway = self._context.gcode_gateway
            if gateway is None:
                await self._record_failure_if_dirty(
                    project_id,
                    dirty,
                    "gcode gateway unavailable",
                )
                return f"{project_id}:failed"

            try:
                factory = self._context.launch_factory

                async def _prune(env: Mapping[str, str] | None) -> GcodeCommandResult:
                    return await gateway.prune_project_for_maintenance(
                        Path(root_path).expanduser(),
                        retention_days=resolved_retention,
                        timeout=CODE_INDEX_PRUNE_TIMEOUT_SECONDS,
                        env=env,
                    )

                if factory is None:
                    command_result = await _prune(None)
                else:
                    async with open_launch_async(
                        factory,
                        project_id,
                        timeout_seconds=CODE_INDEX_PRUNE_TIMEOUT_SECONDS,
                    ) as launch:
                        command_result = await _prune(launch.env)
                if command_result.timed_out:
                    status = "timed_out"
                elif command_result.success or _is_noop_shutdown_result(command_result):
                    status = "completed"
                else:
                    status = "failed"
                log_gcode_maintenance_event(
                    log_file=_maintenance_log_file(self._context),
                    event="targeted_prune",
                    run_id=run_id or uuid4().hex,
                    project_id=project_id,
                    root_path=str(Path(root_path).expanduser()),
                    result=command_result,
                    status=status,
                    detail=reason,
                )
                if status != "completed":
                    detail = (
                        command_result.stderr.strip()
                        or command_result.stdout.strip()
                        or "gcode prune failed"
                    )
                    raise RuntimeError(detail)
            except Exception as exc:
                if _is_noop_shutdown_prune(exc):
                    logger.debug(
                        "Code index prune was interrupted after no-op result for %s at %s",
                        project_id,
                        root_path,
                    )
                    await self._context.run_db(
                        self._context.storage.clear_prune_dirty,
                        project_id,
                    )
                    return f"{project_id}:pruned"
                await self._record_failure_if_dirty(project_id, dirty, str(exc))
                logger.warning(
                    "Code index prune failed for %s at %s: %s",
                    project_id,
                    root_path,
                    exc,
                    exc_info=True,
                )
                return f"{project_id}:failed"

            await self._context.run_db(self._context.storage.clear_prune_dirty, project_id)
            logger.debug("Code index prune completed for %s (%s)", project_id, reason)
            return f"{project_id}:pruned"

    async def _record_failure_if_dirty(self, project_id: str, dirty: bool, error: str) -> None:
        if dirty:
            await self._context.run_db(
                self._context.storage.record_prune_failure, project_id, error
            )

    async def _clear_dirty_projects(self) -> None:
        dirty_projects = await self._context.run_db(
            self._context.storage.list_prune_dirty_projects,
            1000,
        )
        for dirty in dirty_projects:
            await self._context.run_db(
                self._context.storage.clear_prune_dirty,
                dirty.project_id,
            )


def create_code_index_prune_handler(pruner: CodeIndexPruner) -> CronHandler:
    async def _handler(_job: CronJob) -> CodeIndexPruneResult:
        return await pruner.prune_all_projects()

    return _handler


def register_code_index_prune_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    pruner: CodeIndexPruner,
    project_id: str | None,
) -> None:
    """Register the global hourly code-index prune system cron job."""
    cron_executor.register_handler(
        CODE_INDEX_PRUNE_HANDLER, create_code_index_prune_handler(pruner)
    )
    action_config = {
        "handler": CODE_INDEX_PRUNE_HANDLER,
        "purpose": CODE_INDEX_PRUNE_DESCRIPTION,
    }
    existing = cron_storage.get_job_by_name(CODE_INDEX_PRUNE_JOB_NAME)
    if existing is None:
        cron_storage.create_job(
            project_id=project_id or PERSONAL_PROJECT_ID,
            name=CODE_INDEX_PRUNE_JOB_NAME,
            description=CODE_INDEX_PRUNE_DESCRIPTION,
            schedule_type="interval",
            interval_seconds=CODE_INDEX_PRUNE_INTERVAL_SECONDS,
            action_type="handler",
            action_config=action_config,
            enabled=True,
            is_system=True,
        )
        return

    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)

    repaired = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=action_config,
        description=CODE_INDEX_PRUNE_DESCRIPTION,
        schedule_type="interval",
        interval_seconds=CODE_INDEX_PRUNE_INTERVAL_SECONDS,
    )
    if repaired is not None and repaired.enabled and repaired.next_run_at is None:
        cron_storage.wake_system_job(repaired.id)


def _dirty_prune_cursor(dirty: Any) -> tuple[Any, Any, str]:
    return (dirty.updated_at, dirty.created_at, dirty.project_id)


def _maintenance_log_file(context: CodeIndexContext) -> str:
    return str(
        getattr(
            getattr(context, "config", object()),
            "maintenance_log_file",
            _DEFAULT_MAINTENANCE_LOG_FILE,
        )
    )
