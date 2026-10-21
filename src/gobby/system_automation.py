"""Daemon-owned stale-claim sweeps and pipeline maintenance."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from gobby.config.app import DaemonConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._automation import sweep_stale_claims

logger = logging.getLogger(__name__)

AUTOMATION_TICK_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class AutomationLoopSettings:
    """Resolved automation loop settings."""

    enabled: bool = True
    interval_seconds: int = 60


@dataclass(frozen=True)
class AutomationMaintenanceSummary:
    """Counts from maintenance run by the automation loop."""

    stale_claims_released: int = 0
    pipeline_stalled_handled: int = 0
    pipeline_stale_tasks_recovered: int = 0
    pipeline_running_executions: int = 0
    pipeline_stale_task_candidates: int = 0


@dataclass
class AutomationTickSummary:
    """Lightweight in-memory result for one automation loop tick."""

    reason: str
    maintenance: AutomationMaintenanceSummary = field(default_factory=AutomationMaintenanceSummary)
    duration_ms: float = 0.0
    error: str | None = None


class PipelineHeartbeatService(Protocol):
    """Pipeline maintenance operations used by the automation loop."""

    async def check_stalled_executions(self) -> int: ...

    async def check_stale_tasks(self) -> tuple[int, int]: ...

    async def count_running_executions(self) -> int: ...


class SystemAutomationLoop:
    """Run daemon-owned claim and pipeline maintenance."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        capture_bundle: Callable[[], RuntimeActiveBundle],
        services: object | None = None,
        pipeline_heartbeat: PipelineHeartbeatService | None = None,
        run_db: Any | None = None,
    ) -> None:
        self.db = db
        self._capture_bundle = capture_bundle
        self.services = services
        self.pipeline_heartbeat = pipeline_heartbeat
        self._db_runner = run_db or getattr(services, "run_db", None)
        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._tick_lock = asyncio.Lock()
        self._tick_count = 0
        self._last_settings = AutomationLoopSettings()
        self._last_tick: AutomationTickSummary | None = None
        self._last_tick_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None

    async def start(self) -> None:
        """Start the automation loop."""
        if self._running:
            return
        self._running = True
        self._last_settings = self.resolve_settings()
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
        """Stop the automation loop."""
        self._running = False
        task = self._loop_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._loop_task = None
        logger.info("System automation loop stopped")

    def set_services(self, services: object) -> None:
        self.services = services
        self._db_runner = getattr(services, "run_db", None) or self._db_runner

    async def run_once(self, *, reason: str = "interval") -> AutomationTickSummary:
        """Run one bounded maintenance tick."""
        return await self._run_once_with_settings(self.resolve_settings(), reason=reason)

    async def _run_once_with_settings(
        self,
        settings: AutomationLoopSettings,
        *,
        reason: str,
    ) -> AutomationTickSummary:
        started = time.perf_counter()
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
                stale_claims_released = await self._run_claim_maintenance()
                pipeline = await self._run_pipeline_maintenance()
                maintenance = AutomationMaintenanceSummary(
                    stale_claims_released=stale_claims_released,
                    pipeline_stalled_handled=pipeline.pipeline_stalled_handled,
                    pipeline_stale_tasks_recovered=pipeline.pipeline_stale_tasks_recovered,
                    pipeline_running_executions=pipeline.pipeline_running_executions,
                    pipeline_stale_task_candidates=pipeline.pipeline_stale_task_candidates,
                )
                summary = AutomationTickSummary(reason=reason, maintenance=maintenance)
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

    def resolve_settings(self) -> AutomationLoopSettings:
        return self._settings_from_config(self._capture_bundle().snapshot.active)

    def status_snapshot(self) -> dict[str, Any]:
        """Return lightweight service status for admin status endpoints."""
        return {
            "enabled": self._last_settings.enabled,
            "running": self._running,
            "interval_seconds": self._last_settings.interval_seconds,
            "tick_count": self._tick_count,
            "last_tick_at": self._last_tick_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "last_tick": asdict(self._last_tick) if self._last_tick is not None else None,
            "tick_timeout_seconds": AUTOMATION_TICK_TIMEOUT_SECONDS,
        }

    async def _run_loop(self) -> None:
        while self._running:
            settings = self.resolve_settings()
            self._last_settings = settings
            if settings.enabled:
                await self._run_once_with_settings(settings, reason="interval")
            try:
                await asyncio.sleep(settings.interval_seconds)
            except asyncio.CancelledError:
                break

    async def _run_claim_maintenance(self) -> int:
        return int(await self._run_db_call(sweep_stale_claims, self.db) or 0)

    async def _run_pipeline_maintenance(self) -> AutomationMaintenanceSummary:
        heartbeat = self.pipeline_heartbeat
        if heartbeat is None:
            return AutomationMaintenanceSummary()
        stalled = await heartbeat.check_stalled_executions()
        recovered, stale_candidates = await heartbeat.check_stale_tasks()
        running = await heartbeat.count_running_executions()
        return AutomationMaintenanceSummary(
            pipeline_stalled_handled=int(stalled or 0),
            pipeline_stale_tasks_recovered=int(recovered or 0),
            pipeline_running_executions=int(running or 0),
            pipeline_stale_task_candidates=int(stale_candidates or 0),
        )

    async def _run_db_call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        if self._db_runner is not None:
            return await self._db_runner(func, *args, **kwargs)
        return await asyncio.to_thread(func, *args, **kwargs)

    @staticmethod
    def _settings_from_config(config: DaemonConfig) -> AutomationLoopSettings:
        automation = config.system_loops.automation
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
