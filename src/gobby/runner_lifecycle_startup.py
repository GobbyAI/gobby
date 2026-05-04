"""Startup progress and provider catalog helpers for the daemon lifecycle."""

from __future__ import annotations

import asyncio
import inspect
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


def _log_subsystem_init_result(task: asyncio.Task[None]) -> None:
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


async def _refresh_provider_model_catalog(
    provider_catalog: Any,
    codex_client: Any,
) -> dict[str, dict[str, Any]]:
    """Refresh provider models when the catalog exposes an async refresh API."""
    refresh = getattr(provider_catalog, "refresh", None)
    if not callable(refresh):
        return {}

    result = refresh(codex_client=codex_client)
    if not inspect.isawaitable(result):
        logger.debug("Provider model catalog refresh skipped: refresh() is not awaitable")
        return {}

    refreshed = await result
    return refreshed if isinstance(refreshed, dict) else {}


def _record_provider_model_refresh_result(
    task: asyncio.Future[dict[str, dict[str, Any]]],
    tracker: StartupTracker | None,
) -> None:
    """Record optional provider model refresh outcome without blocking startup."""
    if task.cancelled():
        return
    try:
        status = task.result()
    except Exception as e:
        logger.debug("Provider model discovery failed in background: %s", e)
        if tracker:
            tracker.error("Provider models", str(e))
        return

    if tracker:
        tracker.complete("Provider model catalogs updated")
        for provider, info in status.items():
            source = info.get("source", "failed")
            error = info.get("error")
            if source == "live":
                continue
            if source == "cache":
                tracker.error(
                    f"Provider models ({provider})",
                    f"using cache: {error or 'live probe failed'}",
                )
            else:
                tracker.error(
                    f"Provider models ({provider})",
                    error or "model discovery failed",
                )
