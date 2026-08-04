"""Event-loop-independent database saturation diagnostics."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from gobby.storage.concurrency import CoverageExecutor, DatabaseConcurrencyResolution
from gobby.storage.executor import DatabaseExecutor
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.telemetry.instruments import inc_counter, set_gauge

logger = logging.getLogger(__name__)


@dataclass
class _SaturationState:
    since: float | None = None
    last_warning: float | None = None
    peak_waiters: int = 0


class DatabaseSaturationWatchdog:
    """Poll in-memory queues even when the daemon event loop is stalled."""

    def __init__(
        self,
        database: PostgresHubDatabase,
        executor: DatabaseExecutor,
        coverage: CoverageExecutor,
        resolution: DatabaseConcurrencyResolution,
        *,
        poll_seconds: float = 1.0,
        warning_after_seconds: float = 2.0,
        repeat_seconds: float = 10.0,
    ) -> None:
        self.database = database
        self.executor = executor
        self.coverage = coverage
        self.resolution = resolution
        self.poll_seconds = poll_seconds
        self.warning_after_seconds = warning_after_seconds
        self.repeat_seconds = repeat_seconds
        self._states = {name: _SaturationState() for name in ("executor", "pool", "coverage")}
        self._last_saturation: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="gobby-db-watchdog",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(2.0, self.poll_seconds * 2))

    def _run(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.sample()
            except Exception:
                logger.exception("Database saturation watchdog sample failed")

    def sample(self) -> None:
        executor = self.executor.stats()
        coverage = self.coverage.stats()
        pool = self.database.pool_stats()
        pool_waiting = _stat_int(pool, "requests_waiting")
        pool_size = _stat_int(pool, "pool_size")
        pool_available = _stat_int(pool, "pool_available")

        gauges = {
            "database_executor_active": executor.active,
            "database_executor_queued": executor.queued,
            "database_executor_oldest_queue_age_seconds": executor.oldest_queue_seconds,
            "database_pool_size": pool_size,
            "database_pool_available": pool_available,
            "database_pool_waiting": pool_waiting,
            "database_coverage_active": coverage.active,
            "database_coverage_waiting": coverage.waiting,
            "database_coverage_oldest_wait_age_seconds": coverage.oldest_wait_seconds,
        }
        for name, value in gauges.items():
            set_gauge(name, float(value))

        self._update_boundary(
            "executor",
            executor.queued,
            executor.oldest_queue_seconds,
            executor.active,
            executor.max_workers,
        )
        self._update_boundary(
            "pool", pool_waiting, 0.0, pool_size - pool_available, pool_size, pool=pool
        )
        self._update_boundary(
            "coverage",
            coverage.waiting,
            coverage.oldest_wait_seconds,
            coverage.active,
            coverage.max_concurrency,
        )

    def _update_boundary(
        self,
        boundary: str,
        waiting: int,
        oldest_wait: float,
        active: int,
        limit: int,
        *,
        pool: dict[str, Any] | None = None,
    ) -> None:
        now = time.monotonic()
        state = self._states[boundary]
        if waiting <= 0:
            self._recover(boundary, state, now)
            return

        if state.since is None:
            state.since = now
        state.peak_waiters = max(state.peak_waiters, waiting)
        duration = now - state.since
        if duration < self.warning_after_seconds:
            return
        phase = "start" if state.last_warning is None else "repeat"
        if state.last_warning is not None and now - state.last_warning < self.repeat_seconds:
            return
        state.last_warning = now
        inc_counter(
            "database_saturation_events_total",
            attributes={"boundary": boundary, "phase": phase},
        )
        logger.warning(
            "Database saturation boundary=%s phase=%s limit=%s active=%s waiting=%s "
            "oldest_wait_seconds=%.3f pool_stats=%s",
            boundary,
            phase,
            limit,
            active,
            waiting,
            oldest_wait,
            pool or {},
        )
        self._last_saturation = {
            "boundary": boundary,
            "duration_seconds": duration,
            "peak_waiters": state.peak_waiters,
            "recovered": False,
        }

    def _recover(self, boundary: str, state: _SaturationState, now: float) -> None:
        if state.since is not None and state.last_warning is not None:
            duration = now - state.since
            inc_counter(
                "database_saturation_events_total",
                attributes={"boundary": boundary, "phase": "recovered"},
            )
            logger.info(
                "Database saturation recovered boundary=%s duration_seconds=%.3f peak_waiters=%s",
                boundary,
                duration,
                state.peak_waiters,
            )
            self._last_saturation = {
                "boundary": boundary,
                "duration_seconds": duration,
                "peak_waiters": state.peak_waiters,
                "recovered": True,
            }
        self._states[boundary] = _SaturationState()

    def status_snapshot(self) -> dict[str, Any]:
        current = {
            name: {
                "active": state.since is not None,
                "duration_seconds": (
                    time.monotonic() - state.since if state.since is not None else 0.0
                ),
                "peak_waiters": state.peak_waiters,
            }
            for name, state in self._states.items()
        }
        return {
            "sizing": self.resolution.as_dict(),
            "executor": self.executor.stats().as_dict(),
            "pool": self.database.pool_stats(),
            "coverage": self.coverage.stats().as_dict(),
            "saturation": {"current": current, "last": self._last_saturation},
            "restart_required": True,
        }


def _stat_int(stats: dict[str, Any], name: str) -> int:
    value = stats.get(name, 0)
    return value if isinstance(value, int) else 0


__all__ = ["DatabaseSaturationWatchdog"]
