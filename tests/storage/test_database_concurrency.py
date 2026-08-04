from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gobby.config.app import load_config
from gobby.config.database_concurrency import DatabaseConcurrencyConfig
from gobby.storage.concurrency import (
    CoverageExecutor,
    CoverageExecutorStats,
    PostgresCapacity,
    resolve_database_concurrency,
)
from gobby.storage.concurrency_watchdog import DatabaseSaturationWatchdog
from gobby.storage.executor import DatabaseExecutorStats


def test_shared_database_concurrency_vectors() -> None:
    contract_path = (
        Path(__file__).parents[2] / "docs" / "contracts" / "database-concurrency-v1.json"
    )
    contract = json.loads(contract_path.read_text())
    assert contract["version"] == 1

    for case in contract["cases"]:
        config = DatabaseConcurrencyConfig.model_validate(case["config"])
        capacity = PostgresCapacity(**case["capacity"])
        if "expected" not in case:
            with pytest.raises(ValueError, match=case["error_contains"]):
                resolve_database_concurrency(config, capacity, cpu_count=case["cpu_count"])
            continue

        resolved = resolve_database_concurrency(config, capacity, cpu_count=case["cpu_count"])
        actual = resolved.as_dict()
        for name, expected in case["expected"].items():
            if name == "hardware_warning":
                assert (resolved.hardware_warning is not None) is expected, case["name"]
            else:
                assert actual[name] == expected, case["name"]


@pytest.mark.parametrize("value", [0, -1, True, "four", 1.5])
def test_database_concurrency_config_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError):
        DatabaseConcurrencyConfig(executor_max_workers=value)  # type: ignore[arg-type]


def test_database_concurrency_loads_from_config_store() -> None:
    class Store:
        def get_all(self) -> dict[str, int]:
            return {
                "database_concurrency.pool_max_size": 72,
                "database_concurrency.executor_max_workers": 40,
                "database_concurrency.coverage_max_concurrency": 8,
            }

    config = load_config(config_store=Store())  # type: ignore[arg-type]

    assert config.database_concurrency == DatabaseConcurrencyConfig(
        pool_max_size=72,
        executor_max_workers=40,
        coverage_max_concurrency=8,
    )


@pytest.mark.asyncio
async def test_coverage_executor_releases_admission_after_caller_cancellation() -> None:
    executor = CoverageExecutor(max_concurrency=1)
    started = threading.Event()
    release = threading.Event()

    def coverage(value: int) -> int:
        if value == 1:
            started.set()
            release.wait(timeout=5)
        return value

    try:
        first = asyncio.create_task(executor.run(coverage, 1))
        assert await asyncio.to_thread(started.wait, 1)
        queued = asyncio.Event()

        async def run_second() -> int:
            queued.set()
            return await executor.run(coverage, 2)

        second = asyncio.create_task(run_second())
        await queued.wait()
        assert executor.stats().waiting == 1

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert executor.stats().active == 1
        assert executor.stats().waiting == 1

        release.set()
        assert await asyncio.wait_for(second, timeout=2) == 2
        assert executor.stats().completed == 2
    finally:
        release.set()
        executor.shutdown()
        executor.join()


class _FakeDatabase:
    def __init__(self) -> None:
        self.waiting = 0

    def pool_stats(self) -> dict[str, int]:
        return {"pool_size": 64, "pool_available": 64, "requests_waiting": self.waiting}


class _FakeExecutor:
    def __init__(self) -> None:
        self.queued = 1

    def stats(self) -> DatabaseExecutorStats:
        return DatabaseExecutorStats(
            max_workers=16,
            active=16,
            queued=self.queued,
            submitted=17,
            completed=0,
            cancelled=0,
            threads=16,
            oldest_queue_seconds=3.0 if self.queued else 0.0,
            shutdown=False,
        )


class _FakeCoverage:
    def stats(self) -> CoverageExecutorStats:
        return CoverageExecutorStats(
            max_concurrency=2,
            active=0,
            waiting=0,
            submitted=0,
            completed=0,
            cancelled=0,
            oldest_wait_seconds=0.0,
            shutdown=False,
        )


def test_watchdog_logs_sustained_saturation_and_recovery(caplog: pytest.LogCaptureFixture) -> None:
    config = DatabaseConcurrencyConfig()
    resolution = resolve_database_concurrency(
        config,
        PostgresCapacity(max_connections=100, superuser_reserved_connections=3),
        cpu_count=8,
    )
    executor = _FakeExecutor()
    watchdog = DatabaseSaturationWatchdog(
        _FakeDatabase(),  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
        _FakeCoverage(),  # type: ignore[arg-type]
        resolution,
        warning_after_seconds=0,
        repeat_seconds=10,
    )

    with caplog.at_level(logging.INFO):
        watchdog.sample()
        executor.queued = 0
        watchdog.sample()

    assert "Database saturation boundary=executor phase=start" in caplog.text
    assert "Database saturation recovered boundary=executor" in caplog.text
    snapshot: dict[str, Any] = watchdog.status_snapshot()
    assert snapshot["saturation"]["last"]["recovered"] is True
    assert snapshot["restart_required"] is True
