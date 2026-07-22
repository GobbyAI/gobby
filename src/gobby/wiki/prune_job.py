"""Global gwiki orphan-scope reconciliation cron job."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, TypedDict, cast

from gobby.gwiki_gateway import GwikiCommandResult, GwikiUnavailableError
from gobby.scheduler.executor import CronHandler
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob

WIKI_PRUNE_JOB_NAME = "gobby:wiki-prune"
WIKI_PRUNE_HANDLER = "wiki:prune"
WIKI_PRUNE_INTERVAL_SECONDS = 3600
WIKI_PRUNE_TIMEOUT_SECONDS = 120
WIKI_PRUNE_DESCRIPTION = "Reconcile orphaned gwiki project state across storage backends"


class WikiPruneResult(TypedDict, total=False):
    success: bool
    status: Literal["completed", "failed", "timed_out", "unavailable"]
    message: str
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    unavailable: bool
    error: str


class WikiPruneGateway(Protocol):
    async def prune_all_scopes(
        self,
        *,
        timeout: float | None = None,
    ) -> GwikiCommandResult: ...


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None: ...


def guard_project_cron_handler[HandlerResult](
    handler: Callable[[CronJob], Awaitable[HandlerResult]],
    project_lookup: Callable[[str], Any | None],
) -> Callable[[CronJob], Awaitable[HandlerResult]]:
    """Skip a handler when its project disappeared or was soft-deleted."""

    async def _guarded(job: CronJob) -> HandlerResult:
        project = await asyncio.to_thread(project_lookup, job.project_id)
        if project is None or project.deleted_at is not None:
            return cast(
                HandlerResult,
                {
                    "success": True,
                    "status": "skipped",
                    "message": (
                        f"project {job.project_id} is absent or deleted; scheduled write skipped"
                    ),
                    "skipped": True,
                },
            )
        return await handler(job)

    return _guarded


def create_wiki_prune_handler(gateway: WikiPruneGateway) -> CronHandler:
    async def _handler(_job: CronJob) -> WikiPruneResult:
        try:
            result = await gateway.prune_all_scopes(timeout=WIKI_PRUNE_TIMEOUT_SECONDS)
        except GwikiUnavailableError as exc:
            return {
                "success": False,
                "status": "unavailable",
                "message": str(exc),
                "unavailable": True,
                "error": str(exc),
            }
        status: Literal["completed", "failed", "timed_out"]
        if result.timed_out:
            status = "timed_out"
        elif result.success:
            status = "completed"
        else:
            status = "failed"
        return {
            "success": result.success,
            "status": status,
            "message": result.stderr.strip() or result.stdout.strip() or f"gwiki prune {status}",
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }

    return _handler


def register_wiki_prune_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    gateway: WikiPruneGateway,
    project_id: str | None,
) -> None:
    """Register and reconcile the global hourly wiki prune system job."""
    cron_executor.register_handler(WIKI_PRUNE_HANDLER, create_wiki_prune_handler(gateway))
    action_config = {
        "handler": WIKI_PRUNE_HANDLER,
        "purpose": WIKI_PRUNE_DESCRIPTION,
    }
    existing = cron_storage.get_job_by_name(WIKI_PRUNE_JOB_NAME)
    if existing is None:
        cron_storage.create_job(
            project_id=project_id or "system",
            name=WIKI_PRUNE_JOB_NAME,
            description=WIKI_PRUNE_DESCRIPTION,
            schedule_type="interval",
            interval_seconds=WIKI_PRUNE_INTERVAL_SECONDS,
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
        description=WIKI_PRUNE_DESCRIPTION,
        schedule_type="interval",
        interval_seconds=WIKI_PRUNE_INTERVAL_SECONDS,
    )
    if reconciled is not None and reconciled.enabled and reconciled.next_run_at is None:
        cron_storage.wake_system_job(reconciled.id)
