"""Startup progress and provider catalog helpers for the daemon lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("gobby.runner_lifecycle")


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
