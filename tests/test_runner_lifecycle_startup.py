"""Restart diagnostics report the task that blocks the daemon loop."""

import asyncio
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
            if "Startup event-loop lag" in record.getMessage():
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
