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
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)

    async def block_loop() -> None:
        assert release.wait(timeout=1)

    try:
        start_startup_lag_probe(
            asyncio.get_running_loop(),
            duration_seconds=0.5,
            interval_seconds=0.02,
            threshold_seconds=0.1,
        )
        await asyncio.create_task(block_loop(), name="blocking-startup-task")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    await _wait_for_probe_shutdown(0.55)
    assert records


@pytest.mark.asyncio
async def test_lag_probe_stays_active_and_rate_limits_repeated_stack_sites(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gobby.runner_lifecycle")
    assert inspect.signature(start_startup_lag_probe).parameters["duration_seconds"].default is None
    assert (
        inspect.signature(start_startup_lag_probe).parameters["threshold_seconds"].default == 0.25
    )
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

    reports = [
        record.message for record in caplog.records if "task=repeated-loop-stall" in record.message
    ]
    assert len(reports) == 1
    assert "task=repeated-loop-stall" in reports[0]
    assert "stack=" in reports[0]


@pytest.mark.asyncio
async def test_subsecond_lag_records_histogram_and_logs_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gobby.runner_lifecycle")
    with patch("gobby.runner_lifecycle_startup.observe_histogram") as histogram:
        start_startup_lag_probe(
            asyncio.get_running_loop(),
            duration_seconds=0.6,
            interval_seconds=0.01,
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
    assert any("task=subsecond-loop-stall" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_default_lag_probe_reports_320ms_stall_at_debug_without_idle_report(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gobby.runner_lifecycle")
    start_startup_lag_probe(asyncio.get_running_loop(), duration_seconds=0.7)

    async def block_loop() -> None:
        threading.Event().wait(0.32)

    await asyncio.create_task(block_loop(), name="short-default-stall")
    await _wait_for_probe_shutdown(0.4)

    reports = [
        record.message for record in caplog.records if "Daemon event-loop lag" in record.message
    ]
    assert len(reports) == 1
    assert "task=short-default-stall" in reports[0]
    assert "stack=" in reports[0]


def test_lag_probe_skips_stack_inspection_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="gobby.runner_lifecycle")
    logger = logging.getLogger("gobby.runner_lifecycle")
    loop = asyncio.new_event_loop()
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    try:
        with (
            patch.object(loop, "is_running", return_value=True),
            patch("gobby.runner_lifecycle_startup.threading.Thread") as probe_thread,
            patch("gobby.runner_lifecycle_startup.time.monotonic", side_effect=lambda: clock[0]),
            patch("gobby.runner_lifecycle_startup.time.sleep", side_effect=advance),
            patch(
                "gobby.runner_lifecycle_startup.sys._current_frames",
                side_effect=AssertionError("stack inspection at INFO"),
            ) as current_frames,
            patch.object(logger, "isEnabledFor", wraps=logger.isEnabledFor) as level_check,
        ):
            start_startup_lag_probe(
                loop,
                duration_seconds=0.4,
                interval_seconds=0.05,
                threshold_seconds=0.25,
            )
            probe_thread.call_args.kwargs["target"]()
        assert clock[0] >= 0.4
        level_check.assert_called_once_with(logging.DEBUG)
        current_frames.assert_not_called()
        assert not any("Daemon event-loop lag" in record.message for record in caplog.records)
    finally:
        loop.close()


def test_lag_probe_does_not_report_after_loop_stops_before_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gobby.runner_lifecycle")
    loop = asyncio.new_event_loop()
    clock = [0.0]

    def advance(seconds: float) -> None:
        clock[0] += seconds

    try:
        with (
            patch("gobby.runner_lifecycle_startup.threading.Thread") as probe_thread,
            patch("gobby.runner_lifecycle_startup.time.monotonic", side_effect=lambda: clock[0]),
            patch("gobby.runner_lifecycle_startup.time.sleep", side_effect=advance),
        ):
            start_startup_lag_probe(
                loop,
                duration_seconds=0.4,
                interval_seconds=0.05,
                threshold_seconds=0.25,
            )
            watch = probe_thread.call_args.kwargs["target"]
            watch()
    finally:
        loop.close()

    assert not any("Daemon event-loop lag" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_lag_reports_when_stall_grows_past_threshold_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gobby.runner_lifecycle")
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
