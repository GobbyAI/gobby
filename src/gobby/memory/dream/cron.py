"""System cron registration for memory dream."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from gobby.config.persistence import MemoryDreamConfig
from gobby.memory.dream.protocols import MemoryDreamManagerProtocol
from gobby.memory.dream.storage import MemoryDreamStore
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob
from gobby.storage.projects import PERSONAL_PROJECT_ID

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.memory.dream.coordinator import MemoryDreamCoordinator

MEMORY_DREAM_CRON_JOB_NAME = "gobby:memory-dream"
MEMORY_DREAM_CRON_HANDLER = "memory.dream"
MEMORY_DREAM_CRON_DESCRIPTION = "Scheduled memory dream review and consolidation"
# Slack for the terminal run write once the last admitted work unit returns.
MEMORY_DREAM_CRON_FINALIZE_GRACE_SECONDS = 300.0

CronHandler = Callable[[CronJob], Awaitable[str]]


def _action_config(dream_config: MemoryDreamConfig) -> dict[str, str | float | bool]:
    """Build the handler action config, including its own bounded timeout.

    The handler runs the sweep inline, so the cron executor's bounded-action
    timeout has to clear the coordinator's own ceiling: the admission window
    plus the final admitted work unit. Without an explicit value the executor
    falls back to ``cron.running_timeout_seconds`` (1440s by default), which is
    shorter than a single work unit and cancels every real nightly sweep.
    """
    return {
        "handler": MEMORY_DREAM_CRON_HANDLER,
        "timeout_seconds": (
            float(dream_config.max_runtime_seconds)
            + float(dream_config.work_unit_timeout_seconds)
            + MEMORY_DREAM_CRON_FINALIZE_GRACE_SECONDS
        ),
        # A nightly sweep runs for hours; its running cron row is the restart
        # lease that `gobby stop`/`restart` honor unless forced.
        "restart_protected": True,
    }


class CronRegistrationProtocol(Protocol):
    def register_handler(self, name: str, handler: CronHandler) -> None:
        """Register a cron handler by name."""
        ...


def reconcile_interrupted_dream_runs(memory_manager: MemoryDreamManagerProtocol) -> list[str]:
    """Mark dream runs orphaned by a daemon restart as 'interrupted'.

    Runs once during synchronous startup (init_orchestration) before the daemon
    serves requests, so any non-terminal run is necessarily orphaned. Mirrors the
    agent-run restart reconciliation; not gated on whether dreaming is enabled so
    that orphans are cleaned up even after the feature is turned off. Returns the
    reconciled run IDs.
    """
    store = MemoryDreamStore(memory_manager.db)
    return store.mark_interrupted_runs()


def register_memory_dream_cron(
    *,
    cron_storage: CronJobStorage,
    cron_executor: CronRegistrationProtocol,
    coordinator: MemoryDreamCoordinator,
    dream_config: MemoryDreamConfig,
    project_id: str | None = None,
) -> int:
    """Register the memory dream handler and reconcile its single system row."""
    if not dream_config.enabled:
        existing = cron_storage.get_job_by_name(MEMORY_DREAM_CRON_JOB_NAME)
        if existing and existing.enabled:
            updated = cron_storage.update_job(existing.id, enabled=False, next_run_at=None)
            if updated is None:
                logger.warning(
                    "System cron job already disappeared during disable: %s",
                    MEMORY_DREAM_CRON_JOB_NAME,
                )
        return 0

    async def _handler(_job: CronJob) -> str:
        # Cooldown-throttled nightly sweep: round-robin work units across every
        # scope with due memories via the daemon-owned coordinator's service
        # (the same admission owner behind manual triggers). Nightly mutating
        # maintenance is the default; the admission window bounds the run and a
        # window-exhausted partial is a normal outcome. Admission owns the
        # aggregate row, so a fire that overlaps an active run coalesces or
        # skips instead of stacking a second sweep. Unlike HTTP/MCP triggers,
        # cron executes inline: the handler already runs as its own background
        # job and reports the completed aggregate.
        service = coordinator.service
        started = await service.start_all_due_projects_async(dry_run=False)
        if started.get("coalesced"):
            run_id = started.get("run_id")
            logger.info("Memory dream cron coalesced onto active run %s", run_id)
            return f"memory dream coalesced onto active run {run_id}"
        if not started.get("success"):
            conflict = started.get("conflict")
            if conflict is not None:
                logger.info(
                    "Memory dream cron skipped; incompatible active run %s (scope=%s phase=%s)",
                    conflict.get("run_id"),
                    conflict.get("scope"),
                    conflict.get("phase"),
                )
                return f"memory dream skipped: active run {conflict.get('run_id')}"
            raise RuntimeError(str(started.get("error", "memory dream admission failed")))
        result = await service.execute_all_due_projects_run(str(started["run_id"]), dry_run=False)
        if not result.get("success"):
            raise RuntimeError("memory dream failed for all targets")
        aggregate = result.get("aggregate") or {}
        completed = int(aggregate.get("completed", 0))
        mutations = int(aggregate.get("mutations", 0))
        failed = int(aggregate.get("failed", 0))
        stop_reason = str(aggregate.get("stop_reason") or "drained")
        tail = f", {failed} failed" if failed else ""
        # Early stops are already logged by the coordinator (INFO for window
        # exhaustion, one WARNING for dependency failure) — report, don't warn.
        if stop_reason != "drained":
            tail += f", stopped: {stop_reason}"
        return f"memory dream: {completed} target(s), {mutations} mutation(s) total{tail}"

    cron_executor.register_handler(MEMORY_DREAM_CRON_HANDLER, _handler)
    _ensure_system_job(cron_storage, dream_config, project_id)
    return 1


def _ensure_system_job(
    cron_storage: CronJobStorage,
    dream_config: MemoryDreamConfig,
    project_id: str | None,
) -> None:
    existing = cron_storage.get_job_by_name(MEMORY_DREAM_CRON_JOB_NAME)
    cron_expr = str(getattr(dream_config, "schedule_cron", "0 2 * * *"))
    target_project_id = project_id or PERSONAL_PROJECT_ID
    if existing is None:
        cron_storage.create_job(
            project_id=target_project_id,
            name=MEMORY_DREAM_CRON_JOB_NAME,
            description=MEMORY_DREAM_CRON_DESCRIPTION,
            schedule_type="cron",
            cron_expr=cron_expr,
            action_type="handler",
            action_config=_action_config(dream_config),
            enabled=True,
            is_system=True,
        )
        logger.info("Created system cron job: %s", MEMORY_DREAM_CRON_JOB_NAME)
        return

    if not existing.is_system:
        cron_storage.mark_as_system_job(existing.id)
    was_enabled = existing.enabled
    repaired = cron_storage.reconcile_system_job_definition(
        existing.id,
        action_type="handler",
        action_config=_action_config(dream_config),
        description=MEMORY_DREAM_CRON_DESCRIPTION,
        schedule_type="cron",
        cron_expr=cron_expr,
        interval_seconds=None,
        run_at=None,
    )
    if repaired and was_enabled and not repaired.enabled:
        cron_storage.wake_system_job(repaired.id)
