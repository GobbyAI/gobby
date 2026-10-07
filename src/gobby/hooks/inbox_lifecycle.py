"""App-scoped ownership of replay tasks that outlive a barrier wait."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, cast

from gobby.hooks.background_tasks import create_background_task

logger = logging.getLogger(__name__)


def replay_stopping(app: Any) -> bool:
    return bool(getattr(app.state, "hook_inbox_stopping", False))


def start_replay(app: Any, operation: Coroutine[Any, Any, int]) -> asyncio.Task[int]:
    tasks = getattr(app.state, "hook_inbox_replays", None)
    if tasks is None:
        tasks = set()
        app.state.hook_inbox_replays = tasks
    owned = cast(set[asyncio.Task[int]], tasks)
    task = create_background_task(operation)
    owned.add(task)
    task.add_done_callback(owned.discard)
    return task


async def stop_hook_inbox_replays(app: Any, *, timeout_seconds: float = 2.0) -> None:
    """Close replay admission and settle owned cancellation before storage teardown."""
    app.state.hook_inbox_stopping = True
    tasks = tuple(getattr(app.state, "hook_inbox_replays", ()))
    for task in tasks:
        task.cancel()
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
        if pending:
            logger.warning("%d hook replay task(s) remain at shutdown deadline", len(pending))
            raise TimeoutError(f"{len(pending)} hook replay tasks did not stop")
