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
    duration_seconds: float = 25 * 60,
    interval_seconds: float = 0.25,
    threshold_seconds: float = 1.0,
) -> None:
    """Sample loop stalls during restart recovery from an independent thread."""
    deadline = time.monotonic() + duration_seconds
    loop_thread_id = threading.get_ident()
    state = {"last_beat": time.monotonic(), "reported": False}

    def beat() -> None:
        state["last_beat"] = time.monotonic()
        state["reported"] = False
        if time.monotonic() < deadline:
            loop.call_later(interval_seconds, beat)

    def watch() -> None:
        while time.monotonic() < deadline and not loop.is_closed():
            time.sleep(interval_seconds)
            lag = time.monotonic() - state["last_beat"] - interval_seconds
            if lag < threshold_seconds or state["reported"]:
                continue
            task = asyncio.current_task(loop)
            frame = sys._current_frames().get(loop_thread_id)
            stack = (
                " > ".join(
                    f"{entry.filename}:{entry.lineno}:{entry.name}"
                    for entry in traceback.extract_stack(frame, limit=12)
                )
                if frame is not None
                else "unavailable"
            )
            logger.warning(
                "Startup event-loop lag %.3fs | task=%s | stack=%s",
                lag,
                task.get_name() if task is not None else "callback-or-idle",
                stack,
            )
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
