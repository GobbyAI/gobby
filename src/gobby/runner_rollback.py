"""Construction rollback ledger for partially initialized daemon runners."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

_PENDING_ASYNC_ROLLBACK_TASKS: set[asyncio.Task[None]] = set()


def _settled_async_close(task: asyncio.Task[None]) -> None:
    _PENDING_ASYNC_ROLLBACK_TASKS.discard(task)
    try:
        task.result()
    except BaseException:
        logger.warning("Async construction rollback did not settle cleanly", exc_info=True)


def _settle_async_close(awaitable: Coroutine[Any, Any, Any]) -> None:
    """Settle an async close from a synchronous constructor rollback."""

    async def settle_with_timeout() -> None:
        await asyncio.wait_for(awaitable, timeout=5.0)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(settle_with_timeout())
        return

    task = loop.create_task(settle_with_timeout(), name="gobby-init-rollback")
    _PENDING_ASYNC_ROLLBACK_TASKS.add(task)
    task.add_done_callback(_settled_async_close)


def rollback_runner_resources(runner: Any) -> None:
    """Release every known daemon-owned side effect installed during construction."""
    from gobby.agents.terminal_delivery import reset_terminal_delivery_offload
    from gobby.agents.tmux import reset_tmux_globals
    from gobby.app_context import clear_app_context
    from gobby.runner_broadcasting import reset_agent_event_broadcasting
    from gobby.telemetry import shutdown_telemetry
    from gobby.utils.tool_summarizer import reset_summarizer_config

    def settle(name: str, callback: Callable[[], Any]) -> None:
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                _settle_async_close(result)
        except BaseException:
            logger.warning("Runner resource rollback failed for %s", name, exc_info=True)

    settle("app context", clear_app_context)
    settle("terminal delivery offload", reset_terminal_delivery_offload)
    settle("tool summarizer", reset_summarizer_config)
    settle("agent event broadcasting", reset_agent_event_broadcasting)
    settle("tmux globals", reset_tmux_globals)

    http_services = getattr(getattr(runner, "http_server", None), "services", None)
    resources = (
        ("config runtime", getattr(runner, "config_runtime", None)),
        (
            "definition revision listener",
            getattr(runner, "definition_revision_listener", None),
        ),
        ("web chat runtime", getattr(http_services, "web_chat_runtime_manager", None)),
        ("memory manager", getattr(runner, "memory_manager", None)),
        ("vector store", getattr(runner, "vector_store", None)),
    )
    for name, resource in resources:
        close = getattr(resource, "close", None) or getattr(resource, "stop", None)
        if callable(close):
            settle(name, close)

    database_watchdog = getattr(runner, "database_watchdog", None)
    if database_watchdog is not None:
        settle("database watchdog", database_watchdog.stop)
    worktree_delete_executor = getattr(runner, "worktree_delete_executor", None)
    if worktree_delete_executor is not None:
        settle("worktree delete executor revocation", worktree_delete_executor.shutdown)
        settle("worktree delete executor join", worktree_delete_executor.join)
    coverage_executor = getattr(runner, "coverage_executor", None)
    if coverage_executor is not None:
        settle("coverage executor revocation", coverage_executor.shutdown)
        settle("coverage executor join", coverage_executor.join)
    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        settle("database executor revocation", db_executor.shutdown)
        settle("database executor join", db_executor.join)
    database = getattr(runner, "database", None)
    if database is not None:
        settle("database", database.close)
    settle("telemetry", shutdown_telemetry)


__all__ = ["rollback_runner_resources"]
