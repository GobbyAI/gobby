"""Single-daemon database concurrency sizing and bounded coverage execution."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, TypeVar, cast

from gobby.config.database_concurrency import ConcurrencyValue, DatabaseConcurrencyConfig
from gobby.telemetry.instruments import observe_histogram

T = TypeVar("T")

BOOTSTRAP_POOL_SIZE = 2
MIN_POOL_SIZE = 32
MIN_EXECUTOR_WORKERS = 8
MIN_SUPPORTED_CPUS = 8


@dataclass(frozen=True)
class PostgresCapacity:
    """PostgreSQL server-global connection settings used for local sizing."""

    max_connections: int
    superuser_reserved_connections: int
    reserved_connections: int = 0

    @property
    def usable_connections(self) -> int:
        return (
            self.max_connections - self.superuser_reserved_connections - self.reserved_connections
        )


@dataclass(frozen=True)
class DatabaseConcurrencyResolution:
    """Validated effective limits and their resolver inputs."""

    cpu_count: int
    max_connections: int
    superuser_reserved_connections: int
    reserved_connections: int
    usable_connections: int
    pool_budget: int
    pool_max_size: int
    executor_max_workers: int
    coverage_max_concurrency: int
    direct_connection_reserve: int
    bootstrap_pool_size: int
    configured_pool_max_size: ConcurrencyValue
    configured_executor_max_workers: ConcurrencyValue
    configured_coverage_max_concurrency: ConcurrencyValue
    hardware_warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def floor_to_multiple_of_8(value: int) -> int:
    return value - (value % 8)


def resolve_database_concurrency(
    config: DatabaseConcurrencyConfig,
    capacity: PostgresCapacity,
    *,
    cpu_count: int,
) -> DatabaseConcurrencyResolution:
    """Resolve one daemon's limits while preserving server recovery headroom."""
    cpu_count = max(1, cpu_count)
    usable = capacity.usable_connections
    if usable < BOOTSTRAP_POOL_SIZE:
        raise ValueError(
            "PostgreSQL usable connection capacity cannot admit the two-connection bootstrap pool "
            f"(usable={usable})"
        )

    pool_budget = floor_to_multiple_of_8((usable * 3) // 4)
    pool = _configured_or_auto(config.pool_max_size, min(64, pool_budget))
    if pool < MIN_POOL_SIZE:
        raise ValueError(
            f"database_concurrency.pool_max_size resolves to {pool}; at least {MIN_POOL_SIZE} "
            "connections are required"
        )
    if pool > pool_budget:
        raise ValueError(
            f"database_concurrency.pool_max_size={pool} exceeds the single-daemon budget "
            f"of {pool_budget} from {usable} usable PostgreSQL connections"
        )

    coverage = _configured_or_auto(
        config.coverage_max_concurrency,
        min(4, max(1, cpu_count // 4)),
    )
    if not 1 <= coverage <= 8:
        raise ValueError("database_concurrency.coverage_max_concurrency must be between 1 and 8")

    direct_reserve = max(8, pool // 4)
    auto_workers = min(max(cpu_count * 2, MIN_EXECUTOR_WORKERS), 32)
    auto_workers = min(auto_workers, pool - coverage - direct_reserve)
    workers = _configured_or_auto(config.executor_max_workers, auto_workers)
    if workers < MIN_EXECUTOR_WORKERS:
        raise ValueError(
            f"database_concurrency.executor_max_workers resolves to {workers}; at least "
            f"{MIN_EXECUTOR_WORKERS} workers are required"
        )
    if workers + coverage + direct_reserve > pool:
        raise ValueError(
            "database concurrency limits exceed pool capacity: "
            f"workers={workers}, coverage={coverage}, direct_reserve={direct_reserve}, pool={pool}"
        )

    warning = None
    if cpu_count < MIN_SUPPORTED_CPUS:
        warning = (
            f"effective CPU count {cpu_count} is below the supported {MIN_SUPPORTED_CPUS}-CPU "
            "baseline; running with degraded database concurrency"
        )
    return DatabaseConcurrencyResolution(
        cpu_count=cpu_count,
        max_connections=capacity.max_connections,
        superuser_reserved_connections=capacity.superuser_reserved_connections,
        reserved_connections=capacity.reserved_connections,
        usable_connections=usable,
        pool_budget=pool_budget,
        pool_max_size=pool,
        executor_max_workers=workers,
        coverage_max_concurrency=coverage,
        direct_connection_reserve=direct_reserve,
        bootstrap_pool_size=BOOTSTRAP_POOL_SIZE,
        configured_pool_max_size=config.pool_max_size,
        configured_executor_max_workers=config.executor_max_workers,
        configured_coverage_max_concurrency=config.coverage_max_concurrency,
        hardware_warning=warning,
    )


def _configured_or_auto(value: ConcurrencyValue, automatic: int) -> int:
    return automatic if value == "auto" else value


@dataclass(frozen=True)
class CoverageExecutorStats:
    max_concurrency: int
    active: int
    waiting: int
    submitted: int
    completed: int
    cancelled: int
    oldest_wait_seconds: float
    shutdown: bool

    def as_dict(self) -> dict[str, int | float | bool]:
        return asdict(self)


class CoverageExecutor:
    """Cancellation-safe admission in front of a dedicated coverage executor."""

    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self.max_concurrency = max_concurrency
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="gobby-plan-coverage",
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._lock = threading.Lock()
        self._wait_started: dict[int, float] = {}
        self._next_waiter = 0
        self._active = 0
        self._submitted = 0
        self._completed = 0
        self._cancelled = 0
        self._shutdown = False
        self._join_started = False

    async def run(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("CoverageExecutor is shut down")
            waiter_id = self._next_waiter
            self._next_waiter += 1
            self._wait_started[waiter_id] = started
        try:
            await self._semaphore.acquire()
        except BaseException:
            with self._lock:
                self._wait_started.pop(waiter_id, None)
            raise

        wait_seconds = time.monotonic() - started
        with self._lock:
            self._wait_started.pop(waiter_id, None)
            if self._shutdown:
                self._semaphore.release()
                raise RuntimeError("CoverageExecutor is shut down")
            self._active += 1
            self._submitted += 1
        observe_histogram("database_coverage_admission_wait_seconds", wait_seconds)

        context = contextvars.copy_context()
        try:
            future = self._executor.submit(context.run, func, *args, **kwargs)
        except BaseException:
            with self._lock:
                self._active -= 1
            self._semaphore.release()
            raise
        future.add_done_callback(lambda done: self._settle(done, loop))
        return cast(T, await asyncio.wrap_future(future))

    def _settle(self, future: Future[Any], loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._active -= 1
            self._completed += 1
            if future.cancelled():
                self._cancelled += 1
        if not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._semaphore.release)
            except RuntimeError:
                pass

    def stats(self) -> CoverageExecutorStats:
        now = time.monotonic()
        with self._lock:
            oldest = max((now - value for value in self._wait_started.values()), default=0.0)
            return CoverageExecutorStats(
                max_concurrency=self.max_concurrency,
                active=self._active,
                waiting=len(self._wait_started),
                submitted=self._submitted,
                completed=self._completed,
                cancelled=self._cancelled,
                oldest_wait_seconds=oldest,
                shutdown=self._shutdown,
            )

    def shutdown(self, *, cancel_futures: bool = True) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(wait=False, cancel_futures=cancel_futures)

    def join(self) -> None:
        with self._lock:
            if self._join_started:
                return
            self._shutdown = True
            self._join_started = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def is_joined(self) -> bool:
        with self._lock:
            return self._join_started


__all__ = [
    "BOOTSTRAP_POOL_SIZE",
    "CoverageExecutor",
    "CoverageExecutorStats",
    "DatabaseConcurrencyResolution",
    "PostgresCapacity",
    "resolve_database_concurrency",
]
