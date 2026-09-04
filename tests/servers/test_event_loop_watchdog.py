from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import cast
from unittest.mock import MagicMock

from gobby.servers.event_loop_watchdog import EventLoopLagWatchdog


class _StalledLoop:
    def call_soon_threadsafe(self, callback: Callable[..., None], *args: object) -> None:
        self.callback = (callback, args)


class _ResponsiveLoop:
    def __init__(self, delivered: threading.Event) -> None:
        self.delivered = delivered

    def call_soon_threadsafe(self, callback: Callable[..., None], *args: object) -> None:
        callback(*args)
        self.delivered.set()


def test_watchdog_logs_one_warning_with_named_thread_stacks_for_late_tick() -> None:
    stalled_loop = _StalledLoop()
    watchdog_logger = MagicMock(spec=logging.Logger)
    warning_logged = threading.Event()
    watchdog_logger.warning.side_effect = lambda *_args, **_kwargs: warning_logged.set()
    watchdog = EventLoopLagWatchdog(
        cast(asyncio.AbstractEventLoop, stalled_loop),
        threshold_seconds=0.01,
        interval_seconds=0.005,
        watchdog_logger=watchdog_logger,
    )

    watchdog.start()
    assert warning_logged.wait(timeout=1)
    watchdog.stop()

    watchdog_logger.warning.assert_called_once()
    (message,) = watchdog_logger.warning.call_args.args
    extra = watchdog_logger.warning.call_args.kwargs["extra"]
    assert message == "Event loop heartbeat late"
    assert extra["lag_seconds"] > 0.01
    assert extra["main_thread_name"] == threading.main_thread().name
    assert "test_watchdog_logs_one_warning" in extra["main_thread_stack"]
    assert any(
        item["thread_name"] == "gobby-event-loop-lag-watchdog" and item["stack"]
        for item in extra["thread_stacks"]
    )


def test_watchdog_emits_no_warning_for_responsive_loop() -> None:
    heartbeat_delivered = threading.Event()
    watchdog_logger = MagicMock(spec=logging.Logger)
    watchdog = EventLoopLagWatchdog(
        cast(asyncio.AbstractEventLoop, _ResponsiveLoop(heartbeat_delivered)),
        threshold_seconds=1.0,
        interval_seconds=0.005,
        watchdog_logger=watchdog_logger,
    )

    watchdog.start()
    assert heartbeat_delivered.wait(timeout=1)
    watchdog.stop()

    watchdog_logger.warning.assert_not_called()
