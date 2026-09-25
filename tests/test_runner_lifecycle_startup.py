"""Restart diagnostics report the task that blocks the daemon loop."""

import asyncio
import inspect
import logging
import threading
from unittest.mock import patch

import pytest

from gobby.runner_lifecycle_startup import start_startup_lag_probe

pytestmark = pytest.mark.unit


async def _wait_for_probe_shutdown(delay_seconds: float) -> None:
    done = asyncio.Event()
    asyncio.get_running_loop().call_later(delay_seconds, done.set)
    await done.wait()


@pytest.mark.asyncio
async def test_startup_lag_probe_names_blocking_task() -> None:
    release = threading.Event()
    records: list[logging.LogRecord] = []

    class ReleaseOnLag(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "task=blocking-startup-task" in record.getMessage():
                records.append(record)
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
    await _wait_for_probe_shutdown(0.55)
    assert records


@pytest.mark.asyncio
async def test_lag_probe_stays_active_and_rate_limits_repeated_stack_sites(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
    assert inspect.signature(start_startup_lag_probe).parameters["duration_seconds"].default is None
    assert inspect.signature(start_startup_lag_probe).parameters["threshold_seconds"].default == 1.0
    start_startup_lag_probe(
        asyncio.get_running_loop(),
        duration_seconds=1.0,
        interval_seconds=0.01,
        threshold_seconds=0.25,
        rate_limit_seconds=1.0,
    )

    async def block_twice() -> None:
        wait = threading.Event()
        for _ in range(2):
            wait.wait(0.35)
            resumed = asyncio.Event()
            asyncio.get_running_loop().call_later(0.04, resumed.set)
            await resumed.wait()

    await asyncio.create_task(block_twice(), name="repeated-loop-stall")
    await _wait_for_probe_shutdown(0.35)

    warnings = [
        record.message for record in caplog.records if "task=repeated-loop-stall" in record.message
    ]
    assert len(warnings) == 1
    assert "task=repeated-loop-stall" in warnings[0]
    assert "stack=" in warnings[0]


@pytest.mark.asyncio
async def test_subsecond_lag_records_histogram_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
    with patch("gobby.runner_lifecycle_startup.observe_histogram") as histogram:
        start_startup_lag_probe(
            asyncio.get_running_loop(),
            duration_seconds=0.6,
            interval_seconds=0.01,
            threshold_seconds=5.0,
        )

        async def block_loop() -> None:
            threading.Event().wait(0.35)

        await asyncio.create_task(block_loop(), name="subsecond-loop-stall")
        beat_recorded = asyncio.Event()
        asyncio.get_running_loop().call_later(0.03, beat_recorded.set)
        await beat_recorded.wait()
        await _wait_for_probe_shutdown(0.3)

    assert any(
        args.args[0] == "daemon_event_loop_lag_seconds" and args.args[1] >= 0.25
        for args in histogram.call_args_list
    )
    assert not any("task=subsecond-loop-stall" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_lag_warns_when_stall_grows_past_warning_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="gobby.runner_lifecycle")
    start_startup_lag_probe(
        asyncio.get_running_loop(),
        duration_seconds=1.0,
        interval_seconds=0.01,
        threshold_seconds=0.5,
    )

    async def block_loop() -> None:
        threading.Event().wait(0.7)

    await asyncio.create_task(block_loop(), name="growing-loop-stall")
    await _wait_for_probe_shutdown(0.4)

    assert any("task=growing-loop-stall" in record.message for record in caplog.records)
