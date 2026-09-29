"""The background spawn phase of one spawn_agent attempt."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from gobby.mcp_proxy.tools.spawn_agent._failure_cleanup import (
    SpawnCleanupOnce,
    cleanup_failed_spawn,
    remember_spawn_pid,
)

logger = logging.getLogger(__name__)


@dataclass
class SpawnPhase:
    """Execute and finalize one spawn attempt, cleaning a failure through its owner.

    ``execute`` and ``finalize`` are supplied by ``spawn_agent_impl``; ``finalize``
    already carries every finalize keyword except ``spawn_result``.
    """

    runner: Any
    run_id: str
    spawn_request: Any
    execute: Callable[[Any], Awaitable[Any]]
    finalize: Callable[..., Awaitable[dict[str, Any]]]
    handler: Any
    spawn_config: Any
    completion_registry: Any
    cleanup_isolation: bool
    task_manager: Any
    child_session_id: str | None
    spawn_identity: dict[str, Any]
    reasoning: Any
    cleanup_once: SpawnCleanupOnce

    async def fail(self, error: str, *, infrastructure: bool = False) -> dict[str, Any]:
        if infrastructure:
            await asyncio.to_thread(
                self.runner.run_storage.merge_resume_metadata,
                self.run_id,
                {"spawn_retryable_infrastructure": True},
            )
        await cleanup_failed_spawn(
            self.runner,
            self.run_id,
            error,
            self.handler,
            self.spawn_config,
            completion_registry=self.completion_registry,
            cleanup_isolation=self.cleanup_isolation,
            task_manager=self.task_manager,
            child_session_id=self.child_session_id,
            cleanup_once=self.cleanup_once,
        )
        return {
            "success": False,
            "error": error,
            **self.spawn_identity,
            "reasoning": self.reasoning.to_dict(),
        }

    async def execute_phase(self) -> dict[str, Any]:
        try:
            spawn_result = await self.execute(self.spawn_request)
            await asyncio.to_thread(remember_spawn_pid, spawn_result.pid, run_id=self.run_id)
            return await self.finalize(spawn_result=spawn_result)
        except asyncio.CancelledError:
            await self.fail("Agent spawn cancelled")
            raise
        except Exception as exc:
            logger.warning("Agent spawn failed for run %s: %s", self.run_id, type(exc).__name__)
            return await self.fail(str(exc), infrastructure=isinstance(exc, OSError))

    async def run(self) -> None:
        result = await self.execute_phase()
        if not result.get("success"):
            # The error can carry pane output or exception text; the run row keeps it.
            logger.warning("Background agent boot failed for run %s", self.run_id)
