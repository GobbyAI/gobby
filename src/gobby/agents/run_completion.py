"""Shared helpers for agent-run completion and completion-registry wakeups."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.events.completion_registry import CompletionEventRegistry

logger = logging.getLogger(__name__)


async def complete_and_notify_agent_run(
    runner: AgentRunner,
    run_id: str,
    *,
    completion_registry: CompletionEventRegistry | None = None,
    notify_result: dict[str, Any] | None = None,
    completion_result: str | None = None,
    message: str = "",
) -> bool:
    """Mark an agent run complete, then wake any waiters registered on it."""

    completed = await asyncio.to_thread(
        runner.complete_run,
        run_id,
        result=completion_result,
    )

    if not completed:
        logger.debug(
            "Skipping completion notify for run %s; terminal state already recorded",
            run_id,
        )
        return False

    if completion_registry and notify_result is not None:
        try:
            await completion_registry.notify(run_id, notify_result, message=message)
        except Exception:
            logger.debug("Failed to notify completion registry for run %s", run_id, exc_info=True)

    return True
