"""Restart diagnostics report the task that blocks the daemon loop."""

import asyncio
import inspect
import logging
import threading

import pytest

from gobby.runner_lifecycle_startup import start_startup_lag_probe

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_startup_lag_probe_names_blocking_task(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
    release = threading.Event()

    class ReleaseOnLag(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "event-loop lag" in record.getMessage():
                release.set()

    logger = logging.getLogger("gobby.runner_lifecycle")
    handler = ReleaseOnLag()
    logger.addHandler(handler)
    start_startup_lag_probe(
        asyncio.get_running_loop(),
        duration_seconds=0.5,
        interval_seconds=0.02,
        threshold_seconds=0.1,
    )

    async def block_loop() -> None:
        assert release.wait(timeout=1)

    try:
        await asyncio.create_task(block_loop(), name="blocking-startup-task")
    finally:
        logger.removeHandler(handler)
    assert any("task=blocking-startup-task" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_lag_probe_stays_active_and_rate_limits_repeated_stack_sites(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
    assert inspect.signature(start_startup_lag_probe).parameters["duration_seconds"].default is None
    assert (
        inspect.signature(start_startup_lag_probe).parameters["threshold_seconds"].default == 0.25
    )
    start_startup_lag_probe(
        asyncio.get_running_loop(),
        duration_seconds=None,
        interval_seconds=0.01,
        threshold_seconds=0.05,
        rate_limit_seconds=1.0,
    )

    async def block_twice() -> None:
        wait = threading.Event()
        for _ in range(2):
            wait.wait(0.15)
            resumed = asyncio.Event()
            asyncio.get_running_loop().call_later(0.04, resumed.set)
            await resumed.wait()

    await asyncio.create_task(block_twice(), name="repeated-loop-stall")

    warnings = [record.message for record in caplog.records if "event-loop lag" in record.message]
    assert len(warnings) == 1
    assert "task=repeated-loop-stall" in warnings[0]
    assert "stack=" in warnings[0]
