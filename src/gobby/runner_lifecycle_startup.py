"""Startup progress and provider catalog helpers for the daemon lifecycle."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from collections.abc import Awaitable
from typing import Any

from gobby.telemetry.instruments import observe_histogram

logger = logging.getLogger("gobby.runner_lifecycle")


async def timed_startup_phase[T](name: str, operation: Awaitable[T]) -> T:
    """Expose where post-bind startup spends time without changing its ordering."""
    started = time.monotonic()
    try:
        return await operation
    finally:
        logger.info("Startup phase %s took %.3fs", name, time.monotonic() - started)


def start_startup_lag_probe(
    loop: asyncio.AbstractEventLoop,
    *,
    duration_seconds: float | None = None,
    interval_seconds: float = 0.25,
    threshold_seconds: float = 0.25,
    rate_limit_seconds: float = 60.0,
) -> None:
    """Sample loop stalls for the loop lifetime from an independent thread."""
    deadline = time.monotonic() + duration_seconds if duration_seconds is not None else None
    loop_thread_id = threading.get_ident()
    state = {"last_beat": time.monotonic(), "reported": False}
    last_report_by_site: dict[str, float] = {}

    def active() -> bool:
        return not loop.is_closed() and (deadline is None or time.monotonic() < deadline)

    def beat() -> None:
        now = time.monotonic()
        lag = now - state["last_beat"] - interval_seconds
        if lag >= 0.25:
            observe_histogram("daemon_event_loop_lag_seconds", lag)
        state["last_beat"] = now
        state["reported"] = False
        if active():
            loop.call_later(interval_seconds, beat)

    def watch() -> None:
        while active():
            time.sleep(interval_seconds)
            lag = time.monotonic() - state["last_beat"] - interval_seconds
            if lag < threshold_seconds or state["reported"]:
                continue
            task = asyncio.current_task(loop)
            frame = sys._current_frames().get(loop_thread_id)
            stack_entries = traceback.extract_stack(frame, limit=12) if frame is not None else []
            task_frames = task.get_stack(limit=1) if task is not None else []
            site_frame = task_frames[-1] if task_frames else frame
            site_key = (
                f"{site_frame.f_code.co_filename}:{site_frame.f_lineno}:{site_frame.f_code.co_name}"
                if site_frame is not None
                else "unavailable"
            )
            now = time.monotonic()
            if now - last_report_by_site.get(site_key, float("-inf")) >= rate_limit_seconds:
                stack = " > ".join(
                    f"{entry.filename}:{entry.lineno}:{entry.name}" for entry in stack_entries
                )
                logger.warning(
                    "Daemon event-loop lag %.3fs | task=%s | stack=%s",
                    lag,
                    task.get_name() if task is not None else "callback-or-idle",
                    stack or "unavailable",
                )
                last_report_by_site[site_key] = now
            state["reported"] = True

    loop.call_later(interval_seconds, beat)
    threading.Thread(target=watch, name="gobby-startup-lag-probe", daemon=True).start()


class StartupTracker:
    """Tracks subsystem initialization progress for CLI polling."""

    __slots__ = ("steps_completed", "steps_scheduled", "errors", "done", "started_at")

    def __init__(self) -> None:
        self.steps_completed: list[str] = []
        self.steps_scheduled: list[str] = []
        self.errors: list[dict[str, str]] = []
        self.done: bool = False
        self.started_at: float = time.monotonic()

    def complete(self, step: str) -> None:
        self.steps_completed.append(step)

    def schedule(self, step: str) -> None:
        self.steps_scheduled.append(step)

    def error(self, subsystem: str, error: str) -> None:
        self.errors.append({"subsystem": subsystem, "error": error})

    def finish(self) -> None:
        self.done = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps_completed": list(self.steps_completed),
            "steps_scheduled": list(self.steps_scheduled),
            "errors": list(self.errors),
            "done": self.done,
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
        }


def _log_subsystem_init_result(
    task: asyncio.Task[None],
    tracker: StartupTracker | None,
) -> None:
    """Log background subsystem initialization failures as soon as they happen."""
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        logger.error(
            "Subsystem initialization failed",
            exc_info=(type(error), error, error.__traceback__),
        )
        if tracker:
            tracker.error("Subsystem initialization", str(error) or type(error).__name__)
            tracker.finish()
