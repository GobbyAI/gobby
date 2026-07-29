"""Daemon-owned admission and launch owner for memory dream runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from gobby.memory.dream.options import DreamRunOptions
from gobby.memory.dream.service import MemoryDreamService

logger = logging.getLogger(__name__)

DREAM_RUN_CONFLICT = "dream_run_conflict"


class MemoryDreamCoordinator:
    """Single owner of dream-run admission and background execution.

    Constructed once at runner init; cron, HTTP routes, and MCP tools resolve
    this instance instead of building ``MemoryDreamService`` per surface. The
    DB-backed admission row is the concurrency arbiter, so at most one
    background coordinator task is live at a time: a second trigger coalesces
    onto or conflicts with the active run instead of launching another task.
    """

    def __init__(self, service: MemoryDreamService) -> None:
        self.service = service
        self._tasks: set[asyncio.Task[dict[str, Any]]] = set()

    def background_tasks(self) -> tuple[asyncio.Task[dict[str, Any]], ...]:
        """Return the live background run tasks (at most one under admission)."""
        return tuple(self._tasks)

    async def trigger(self, options: DreamRunOptions) -> dict[str, Any]:
        """Admit a scoped run and launch its executor without waiting."""
        started = await self.service.start_async(options)
        return self._resolve(started, lambda run_id: self.service.execute_run(run_id, options))

    async def trigger_all_due_projects(
        self,
        *,
        dry_run: bool = False,
        skip_consolidation: bool = False,
        memory_type: str | None = None,
        full_sweep: bool = False,
    ) -> dict[str, Any]:
        """Admit the aggregate all-due run and launch its executor without waiting."""
        started = await self.service.start_all_due_projects_async(
            dry_run=dry_run,
            skip_consolidation=skip_consolidation,
            memory_type=memory_type,
            full_sweep=full_sweep,
        )
        return self._resolve(
            started,
            lambda run_id: self.service.execute_all_due_projects_run(
                run_id,
                dry_run=dry_run,
                skip_consolidation=skip_consolidation,
                memory_type=memory_type,
                full_sweep=full_sweep,
            ),
        )

    def _resolve(
        self,
        started: dict[str, Any],
        executor: Callable[[str], Coroutine[Any, Any, dict[str, Any]]],
    ) -> dict[str, Any]:
        """Translate an admission outcome into the trigger response contract."""
        if not started.get("success"):
            if started.get("conflict") is not None:
                return {**started, "error_code": DREAM_RUN_CONFLICT}
            return started
        run_id = str(started["run_id"])
        if started.get("coalesced"):
            # An equivalent or covering run is already active; observe it.
            return {**started, "status": "running"}
        failure = self._launch(run_id, executor(run_id))
        if failure is not None:
            return failure
        return {"success": True, "run_id": run_id, "status": "running", "coalesced": False}

    def _launch(
        self, run_id: str, coro: Coroutine[Any, Any, dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Schedule the run executor; on failure leave a terminal failed row."""
        try:
            task = asyncio.create_task(coro, name=f"memory-dream:{run_id}")
        except Exception as exc:
            coro.close()
            error = f"Failed to launch memory dream run: {exc}"
            try:
                self.service.record_run_failure(run_id, error)
            except Exception:
                logger.exception("Failed to record memory dream launch failure")
            return {"success": False, "run_id": run_id, "status": "failed", "error": error}
        self._tasks.add(task)
        task.add_done_callback(lambda done: self._finish(done, run_id))
        return None

    def _finish(self, task: asyncio.Task[dict[str, Any]], run_id: str) -> None:
        self._tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            # Cancellation already marked the run interrupted inside the executor.
            logger.debug("Background memory dream task cancelled for run_id=%s", run_id)
            return
        if exc is not None:
            try:
                self.service.record_run_failure(run_id, f"memory dream run crashed: {exc}")
            except Exception:
                logger.exception("Failed to record memory dream failure for run_id=%s", run_id)
            logger.warning(
                "Background memory dream task failed for run_id=%s",
                run_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        result = task.result()
        if not result.get("success"):
            logger.warning(
                "Background memory dream failed for run_id=%s: %s",
                run_id,
                result.get("error"),
            )

    async def aclose(self) -> None:
        """Cancel and await any live background run task at daemon shutdown."""
        tasks = tuple(self._tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
