"""Thread-based event-loop lag attribution for the daemon."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from types import FrameType

logger = logging.getLogger(__name__)


class EventLoopLagWatchdog:
    """Attribute event-loop heartbeat delays without relying on the stalled loop."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        threshold_seconds: float = 1.0,
        interval_seconds: float = 0.25,
        watchdog_logger: logging.Logger | None = None,
    ) -> None:
        self._loop = loop
        self._threshold_seconds = threshold_seconds
        self._interval_seconds = interval_seconds
        self._logger = watchdog_logger or logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the watchdog once."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="gobby-event-loop-lag-watchdog",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the watchdog and wait briefly for its thread to exit."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._threshold_seconds + self._interval_seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            heartbeat = threading.Event()
            delivered_at: list[float] = []
            scheduled_at = time.monotonic()

            try:
                self._loop.call_soon_threadsafe(
                    _acknowledge_heartbeat,
                    delivered_at,
                    heartbeat,
                )
            except RuntimeError:
                return

            delivered = heartbeat.wait(timeout=self._threshold_seconds)
            lag_seconds = (
                delivered_at[0] - scheduled_at if delivered_at else time.monotonic() - scheduled_at
            )
            if lag_seconds > self._threshold_seconds:
                self._log_lag(lag_seconds)

            if not delivered:
                while not self._stop.is_set() and not heartbeat.wait(self._interval_seconds):
                    pass
            self._stop.wait(self._interval_seconds)

    def _log_lag(self, lag_seconds: float) -> None:
        frames = sys._current_frames()
        main_ident = threading.main_thread().ident
        main_frame = frames.pop(main_ident, None) if main_ident is not None else None
        names = {thread.ident: thread.name for thread in threading.enumerate()}
        thread_stacks = [
            {
                "thread_id": thread_id,
                "thread_name": names.get(thread_id, f"unknown-{thread_id}"),
                "stack": _format_stack(frame),
            }
            for thread_id, frame in frames.items()
        ]
        self._logger.warning(
            "Event loop heartbeat late",
            extra={
                "lag_seconds": lag_seconds,
                "main_thread_name": threading.main_thread().name,
                "main_thread_stack": _format_stack(main_frame),
                "thread_stacks": thread_stacks,
            },
        )


def _format_stack(frame: FrameType | None) -> str:
    if frame is None:
        return "<stack unavailable>"
    return "".join(traceback.format_stack(frame))


def _acknowledge_heartbeat(delivered_at: list[float], heartbeat: threading.Event) -> None:
    delivered_at.append(time.monotonic())
    heartbeat.set()
