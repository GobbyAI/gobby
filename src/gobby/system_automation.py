"""Daemon-owned automation loop for dispatch and pipeline maintenance."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from gobby.build.claim_recovery import recover_safe_build_claims
from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.build.project_state import is_project_automation_enabled
from gobby.config.app import DaemonConfig
from gobby.dispatch.dispatcher import (
    run_heartbeat,
    sweep_orphan_no_run_dispatch_mutexes,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._automation import list_automation_candidates, sweep_stale_claims
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

logger = logging.getLogger(__name__)

AUTOMATION_ENABLED_KEY = "system_loops.automation.enabled"
AUTOMATION_INTERVAL_KEY = "system_loops.automation.interval_seconds"
LEGACY_AUTOMATION_CRON_JOB_NAMES = ("gobby:dispatcher", "gobby:pipeline-heartbeat")
DEFAULT_DIRECT_TICK_BURST = 3
AUTOMATION_TICK_TIMEOUT_SECONDS = 120.0
PROJECT_DISPATCH_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class AutomationLoopSettings:
    """Resolved automation loop settings."""

    enabled: bool = True
    interval_seconds: int = 60


@dataclass(frozen=True)
class AutomationMaintenanceSummary:
    """Counts from non-dispatch maintenance run by the automation loop."""

    safe_claims_considered: int = 0
    safe_claims_released: int = 0
    safe_claims_refused: int = 0
    stale_claims_released: int = 0
    orphan_mutexes_released: int = 0
    pipeline_stalled_handled: int = 0
    pipeline_stale_tasks_recovered: int = 0
    pipeline_running_executions: int = 0
    pipeline_stale_task_candidates: int = 0


@dataclass
class AutomationTickSummary:
    """Lightweight in-memory result for one automation loop tick."""

    reason: str
    projects: list[str] = field(default_factory=list)
    dispatch: dict[str, DispatcherTickSummary] = field(default_factory=dict)
    maintenance: AutomationMaintenanceSummary = field(default_factory=AutomationMaintenanceSummary)
    duration_ms: float = 0.0
    error: str | None = None


@dataclass(frozen=True)
class _PendingProjectDispatch:
    """Coalesced direct wake that arrived while a project dispatch was running."""

    reason: str
    max_ticks: int | None
    max_actions: int | None
    max_active_agents: int | None


class PipelineHeartbeatService(Protocol):
    """Pipeline heartbeat methods used by the automation loop."""

    async def check_stalled_executions(self) -> int: ...

    async def check_stale_tasks(self) -> int: ...

    async def count_running_executions(self) -> int: ...

    async def count_stale_task_candidates(self) -> int: ...


def is_legacy_automation_cron_name(name: str) -> bool:
    """Return whether a cron row belongs to the removed automation mechanism."""
    return name in LEGACY_AUTOMATION_CRON_JOB_NAMES


def remove_legacy_automation_cron_rows(db: HubDatabase) -> int:
    """Delete removed dispatcher and pipeline-heartbeat system cron rows."""
    placeholders = ", ".join("%s" for _ in LEGACY_AUTOMATION_CRON_JOB_NAMES)
    rows = db.fetchall(
        f"SELECT id FROM cron_jobs WHERE name IN ({placeholders})",  # nosec B608
        LEGACY_AUTOMATION_CRON_JOB_NAMES,
    )
    job_ids = [str(row["id"]) for row in rows]
    if not job_ids:
        return 0

    job_placeholders = ", ".join("%s" for _ in job_ids)
    with db.transaction() as conn:
        conn.execute(
            f"DELETE FROM cron_runs WHERE cron_job_id IN ({job_placeholders})",  # nosec B608
            tuple(job_ids),
        )
        cursor = conn.execute(
            f"DELETE FROM cron_jobs WHERE id IN ({job_placeholders})",  # nosec B608
            tuple(job_ids),
        )
    return int(cursor.rowcount or 0)


class SystemAutomationLoop:
    """Daemon-owned loop replacing cron-backed dispatcher automation."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        config: DaemonConfig,
        services: object | None = None,
        config_store: ConfigStore | None = None,
        pipeline_heartbeat: PipelineHeartbeatService | None = None,
        run_db: Any | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.services = services
        self.config_store = config_store
        self.pipeline_heartbeat = pipeline_heartbeat
        self._db_runner = run_db or getattr(services, "run_db", None)
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._project_tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_project_dispatches: dict[str, _PendingProjectDispatch] = {}
        self._tick_lock = asyncio.Lock()
        self._tick_count = 0
        self._dispatch_count = 0
        self._last_settings = self._settings_from_config()
        self._last_tick: AutomationTickSummary | None = None
        self._last_tick_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        """Start the automation loop."""
        if self._running:
            return
        self._event_loop = asyncio.get_running_loop()
        self._running = True
        self._last_settings = await self.resolve_settings()
        self._loop_task = asyncio.create_task(
            self._run_loop(),
            name="system-automation-loop",
        )
        logger.info(
            "System automation loop started (enabled=%s, interval=%ss)",
            self._last_settings.enabled,
            self._last_settings.interval_seconds,
        )

    async def stop(self) -> None:
        """Stop the automation loop and any queued direct ticks."""
        self._running = False
        tasks = [task for task in [self._loop_task] if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._project_tasks:
            self._pending_project_dispatches.clear()
            for task in list(self._project_tasks.values()):
                task.cancel()
            await asyncio.gather(*self._project_tasks.values(), return_exceptions=True)
            self._project_tasks.clear()
        self._loop_task = None
        logger.info("System automation loop stopped")

    def set_services(self, services: object) -> None:
        """Update service container reference after HTTP wiring is complete."""
        self.services = services
        self._db_runner = getattr(services, "run_db", None) or self._db_runner

    def schedule_project_dispatch(
        self,
        *,
        project_id: str | None,
        reason: str,
        max_ticks: int | None = None,
        max_actions: int | None = None,
        max_active_agents: int | None = None,
    ) -> bool:
        """Schedule an in-memory dispatch tick for a project."""
        if not project_id:
            return False
        loop = self._event_loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False
            self._event_loop = loop

        loop.call_soon_threadsafe(
            self._schedule_project_dispatch_on_loop,
            project_id,
            reason,
            max_ticks,
            max_actions,
            max_active_agents,
        )
        return True

    async def run_once(self, *, reason: str = "interval") -> AutomationTickSummary:
        """Run one automation tick now."""
        started = time.perf_counter()
        settings = await self.resolve_settings()
        self._last_settings = settings
        if not settings.enabled:
            summary = AutomationTickSummary(reason=reason, error="automation_disabled")
            self._record_tick(summary, started)
            return summary

        timeout_seconds = AUTOMATION_TICK_TIMEOUT_SECONDS
        try:
            return await asyncio.wait_for(
                self._run_once_enabled(reason=reason, started=started),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            error = f"automation_tick_timeout:{timeout_seconds:g}s"
            logger.error("System automation tick timed out after %ss", timeout_seconds)
            summary = AutomationTickSummary(reason=reason, error=error)
            self._record_tick(summary, started)
            self._last_error = error
            return summary

    async def _run_once_enabled(
        self,
        *,
        reason: str,
        started: float,
    ) -> AutomationTickSummary:
        async with self._tick_lock:
            try:
                maintenance = await self._run_pre_dispatch_maintenance()
                project_ids = await self._dispatchable_project_ids()
                dispatch = await self._dispatch_projects(project_ids, reason=reason)
                pipeline = await self._run_pipeline_maintenance()
                maintenance = AutomationMaintenanceSummary(
                    safe_claims_considered=maintenance.safe_claims_considered,
                    safe_claims_released=maintenance.safe_claims_released,
                    safe_claims_refused=maintenance.safe_claims_refused,
                    stale_claims_released=maintenance.stale_claims_released,
                    orphan_mutexes_released=maintenance.orphan_mutexes_released,
                    pipeline_stalled_handled=pipeline.pipeline_stalled_handled,
                    pipeline_stale_tasks_recovered=pipeline.pipeline_stale_tasks_recovered,
                    pipeline_running_executions=pipeline.pipeline_running_executions,
                    pipeline_stale_task_candidates=pipeline.pipeline_stale_task_candidates,
                )
                summary = AutomationTickSummary(
                    reason=reason,
                    projects=project_ids,
                    dispatch=dispatch,
                    maintenance=maintenance,
                )
                self._record_tick(summary, started)
                self._last_success_at = self._last_tick_at
                self._last_error = None
                return summary
            except Exception as exc:
                logger.exception("System automation tick failed")
                summary = AutomationTickSummary(reason=reason, error=str(exc))
                self._record_tick(summary, started)
                self._last_error = str(exc)
                return summary

    async def dispatch_project_once(
        self,
        *,
        project_id: str,
        reason: str,
        max_ticks: int | None = None,
        max_actions: int | None = None,
        max_active_agents: int | None = None,
    ) -> DispatcherTickSummary:
        """Run a bounded project heartbeat burst without cron bookkeeping."""
        settings = await self.resolve_settings()
        self._last_settings = settings
        if not settings.enabled:
            return DispatcherTickSummary(reason="automation_disabled")
        if not await self._project_automation_enabled(project_id):
            return DispatcherTickSummary(reason="project_automation_paused")

        timeout_seconds = PROJECT_DISPATCH_TIMEOUT_SECONDS
        try:
            return await asyncio.wait_for(
                self._dispatch_project_once_enabled(
                    project_id=project_id,
                    reason=reason,
                    max_ticks=max_ticks,
                    max_actions=max_actions,
                    max_active_agents=max_active_agents,
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            logger.error(
                "System automation project dispatch timed out after %ss",
                timeout_seconds,
                extra={"project_id": project_id, "reason": reason},
            )
            return DispatcherTickSummary(
                reason=(
                    "project_dispatch_timeout:"
                    f"project_id={project_id}:reason={reason}:timeout={timeout_seconds:g}s"
                ),
            )

    async def _dispatch_project_once_enabled(
        self,
        *,
        project_id: str,
        reason: str,
        max_ticks: int | None = None,
        max_actions: int | None = None,
        max_active_agents: int | None = None,
    ) -> DispatcherTickSummary:
        await self._run_db_call(recover_safe_build_claims, self.db, project_id)
        summary = DispatcherTickSummary()
        for _ in range(max_ticks or DEFAULT_DIRECT_TICK_BURST):
            result = await run_heartbeat(
                db=self.db,
                project_id=project_id,
                services=self.services,
                max_actions=max_actions,
                max_active_agents=max_active_agents,
            )
            reason_value = result.reason or (
                "cap_reached" if result.cap_reached else summary.reason
            )
            summary = DispatcherTickSummary(
                ticks=summary.ticks + 1,
                scanned=summary.scanned + result.scanned,
                executed=summary.executed + result.executed,
                skipped=summary.skipped + result.skipped,
                cap_reached=summary.cap_reached or result.cap_reached,
                reason=reason_value,
            )
            if result.executed == 0 or result.cap_reached or result.reason:
                break
        self._dispatch_count += summary.executed
        logger.debug(
            "system_automation_project_dispatch",
            extra={"project_id": project_id, "reason": reason, **asdict(summary)},
        )
        return summary

    async def resolve_settings(self) -> AutomationLoopSettings:
        """Resolve current settings from config_store, falling back to daemon config."""
        settings = self._settings_from_config()
        if self.config_store is None:
            return settings

        enabled = await self._read_config_store_value(AUTOMATION_ENABLED_KEY)
        interval = await self._read_config_store_value(AUTOMATION_INTERVAL_KEY)
        if enabled is not None:
            settings = AutomationLoopSettings(
                enabled=bool(enabled),
                interval_seconds=settings.interval_seconds,
            )
        if interval is not None:
            interval_seconds = _coerce_positive_int(interval)
            if interval_seconds is None:
                logger.warning("Ignoring invalid automation loop interval: %r", interval)
            else:
                settings = AutomationLoopSettings(
                    enabled=settings.enabled,
                    interval_seconds=interval_seconds,
                )
        return settings

    def status_snapshot(self) -> dict[str, Any]:
        """Return lightweight service status for admin status endpoints."""
        last_tick = self._last_tick
        return {
            "enabled": self._last_settings.enabled,
            "running": self._running,
            "interval_seconds": self._last_settings.interval_seconds,
            "tick_count": self._tick_count,
            "dispatch_count": self._dispatch_count,
            "last_tick_at": self._last_tick_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "last_tick": _tick_payload(last_tick) if last_tick is not None else None,
            "pending_projects": sorted(self._project_tasks.keys()),
            "tick_timeout_seconds": AUTOMATION_TICK_TIMEOUT_SECONDS,
            "project_dispatch_timeout_seconds": PROJECT_DISPATCH_TIMEOUT_SECONDS,
        }

    async def _run_loop(self) -> None:
        while self._running:
            settings = await self.resolve_settings()
            self._last_settings = settings
            if settings.enabled:
                await self.run_once(reason="interval")
            try:
                await asyncio.sleep(settings.interval_seconds)
            except asyncio.CancelledError:
                break

    def _schedule_project_dispatch_on_loop(
        self,
        project_id: str,
        reason: str,
        max_ticks: int | None,
        max_actions: int | None,
        max_active_agents: int | None,
    ) -> None:
        existing = self._project_tasks.get(project_id)
        if existing is not None and not existing.done():
            self._pending_project_dispatches[project_id] = _PendingProjectDispatch(
                reason=reason,
                max_ticks=max_ticks,
                max_actions=max_actions,
                max_active_agents=max_active_agents,
            )
            return
        self._start_project_dispatch_task(
            project_id=project_id,
            reason=reason,
            max_ticks=max_ticks,
            max_actions=max_actions,
            max_active_agents=max_active_agents,
        )

    def _start_project_dispatch_task(
        self,
        *,
        project_id: str,
        reason: str,
        max_ticks: int | None,
        max_actions: int | None,
        max_active_agents: int | None,
    ) -> None:
        task = asyncio.create_task(
            self._run_scheduled_project_dispatch(
                project_id=project_id,
                reason=reason,
                max_ticks=max_ticks,
                max_actions=max_actions,
                max_active_agents=max_active_agents,
            ),
            name=f"system-automation-dispatch-{project_id}",
        )
        self._project_tasks[project_id] = task
        task.add_done_callback(
            lambda done_task: self._on_project_dispatch_done(project_id, done_task)
        )

    def _on_project_dispatch_done(self, project_id: str, task: asyncio.Task[None]) -> None:
        if self._project_tasks.get(project_id) is not task:
            return
        self._project_tasks.pop(project_id, None)
        pending = self._pending_project_dispatches.pop(project_id, None)
        if pending is None:
            return
        self._schedule_project_dispatch_on_loop(
            project_id,
            pending.reason,
            pending.max_ticks,
            pending.max_actions,
            pending.max_active_agents,
        )

    async def _run_scheduled_project_dispatch(
        self,
        *,
        project_id: str,
        reason: str,
        max_ticks: int | None,
        max_actions: int | None,
        max_active_agents: int | None,
    ) -> None:
        try:
            await self.dispatch_project_once(
                project_id=project_id,
                reason=reason,
                max_ticks=max_ticks,
                max_actions=max_actions,
                max_active_agents=max_active_agents,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Scheduled system automation dispatch failed",
                extra={"project_id": project_id, "reason": reason},
                exc_info=True,
            )

    async def _run_pre_dispatch_maintenance(self) -> AutomationMaintenanceSummary:
        safe_claims = await self._run_db_call(recover_safe_build_claims, self.db, None)
        stale_claims = await self._run_db_call(sweep_stale_claims, self.db)
        mutex_storage = TaskDispatchMutexManager(self.db)
        orphan_mutexes = await self._run_db_call(
            sweep_orphan_no_run_dispatch_mutexes,
            mutex_storage,
            self.db,
        )
        return AutomationMaintenanceSummary(
            safe_claims_considered=int(getattr(safe_claims, "considered", 0)),
            safe_claims_released=int(getattr(safe_claims, "released", 0)),
            safe_claims_refused=int(getattr(safe_claims, "refused", 0)),
            stale_claims_released=int(stale_claims or 0),
            orphan_mutexes_released=int(orphan_mutexes or 0),
        )

    async def _dispatchable_project_ids(self) -> list[str]:
        candidates = await self._run_db_call(list_automation_candidates, self.db)
        project_ids = {
            str(project_id)
            for project_id in (getattr(candidate, "project_id", None) for candidate in candidates)
            if project_id
        }
        enabled: list[str] = []
        for project_id in sorted(project_ids):
            if await self._project_automation_enabled(project_id):
                enabled.append(project_id)
        return enabled

    async def _dispatch_projects(
        self,
        project_ids: list[str],
        *,
        reason: str,
    ) -> dict[str, DispatcherTickSummary]:
        if not project_ids:
            return {}
        results = await asyncio.gather(
            *[
                self.dispatch_project_once(
                    project_id=project_id,
                    reason=reason,
                    max_ticks=1,
                )
                for project_id in project_ids
            ]
        )
        return dict(zip(project_ids, results, strict=True))

    async def _run_pipeline_maintenance(self) -> AutomationMaintenanceSummary:
        heartbeat = self.pipeline_heartbeat
        if heartbeat is None:
            return AutomationMaintenanceSummary()
        stalled = await heartbeat.check_stalled_executions()
        recovered = await heartbeat.check_stale_tasks()
        running = await heartbeat.count_running_executions()
        stale_candidates = await heartbeat.count_stale_task_candidates()
        return AutomationMaintenanceSummary(
            pipeline_stalled_handled=int(stalled or 0),
            pipeline_stale_tasks_recovered=int(recovered or 0),
            pipeline_running_executions=int(running or 0),
            pipeline_stale_task_candidates=int(stale_candidates or 0),
        )

    async def _project_automation_enabled(self, project_id: str) -> bool:
        return bool(
            await self._run_db_call(
                is_project_automation_enabled,
                self.db,
                project_id,
            )
        )

    async def _read_config_store_value(self, key: str) -> object | None:
        config_store = self.config_store
        if config_store is None:
            return None
        try:
            value: object = await self._run_db_call(config_store.get, key)
            return value
        except Exception:
            logger.warning("Failed to read automation config key %s", key, exc_info=True)
            return None

    async def _run_db_call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        if self._db_runner is not None:
            return await self._db_runner(func, *args, **kwargs)
        return await asyncio.to_thread(func, *args, **kwargs)

    def _settings_from_config(self) -> AutomationLoopSettings:
        automation = self.config.system_loops.automation
        return AutomationLoopSettings(
            enabled=automation.enabled,
            interval_seconds=automation.interval_seconds,
        )

    def _record_tick(self, summary: AutomationTickSummary, started: float) -> None:
        from datetime import UTC, datetime

        summary.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        self._tick_count += 1
        self._last_tick = summary
        self._last_tick_at = datetime.now(UTC).isoformat()


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return max(1, int(value))
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, float):
        return max(1, int(value))
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return max(1, int(value))
        except ValueError:
            return None
    return None


def _tick_payload(summary: AutomationTickSummary) -> dict[str, Any]:
    payload = asdict(summary)
    payload["dispatch"] = {
        project_id: asdict(dispatch_summary)
        for project_id, dispatch_summary in summary.dispatch.items()
    }
    return payload
