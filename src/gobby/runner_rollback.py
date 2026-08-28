"""Construction rollback ledger for partially initialized daemon runners."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


async def _await_named(name: str, callback: Callable[[], Any]) -> None:
    try:
        result = callback()
        if asyncio.iscoroutine(result):
            await result
    except BaseException:
        logger.warning("Runner resource rollback failed for %s", name, exc_info=True)


async def rollback_runner_resources_async(runner: Any) -> None:
    """Ordered async rollback: host producers, drain, clients, then the inventory."""
    host = getattr(runner, "terminal_host_manager", None)
    if host is not None:
        await _await_named("host producers", getattr(host, "stop_producers", lambda: None))
        await _await_named("host rollback", getattr(host, "rollback_host", lambda: None))
        await _await_named("host clients", getattr(host, "close_clients", lambda: None))
    await _rollback_inventory_async(runner)


def rollback_runner_resources(runner: Any) -> None:
    """Release every known daemon-owned side effect installed during construction."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(rollback_runner_resources_async(runner))
        return
    raise RuntimeError(
        "rollback_runner_resources requires no running loop; await rollback_runner_resources_async"
    )


async def _rollback_inventory_async(runner: Any) -> None:
    from gobby.agents.terminal_delivery import reset_terminal_delivery_offload
    from gobby.agents.tmux import reset_tmux_globals
    from gobby.app_context import clear_app_context
    from gobby.runner_broadcasting import reset_agent_event_broadcasting
    from gobby.telemetry import shutdown_telemetry
    from gobby.utils.tool_summarizer import reset_summarizer_config

    await _await_named("app context", clear_app_context)
    await _await_named("terminal delivery offload", reset_terminal_delivery_offload)
    await _await_named("tool summarizer", reset_summarizer_config)
    await _await_named("agent event broadcasting", reset_agent_event_broadcasting)
    await _await_named("tmux globals", reset_tmux_globals)

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
            await _await_named(name, close)

    database_watchdog = getattr(runner, "database_watchdog", None)
    if database_watchdog is not None:
        await _await_named("database watchdog", database_watchdog.stop)
    worktree_delete_executor = getattr(runner, "worktree_delete_executor", None)
    if worktree_delete_executor is not None:
        await _await_named("worktree delete executor revocation", worktree_delete_executor.shutdown)
        await _await_named("worktree delete executor join", worktree_delete_executor.join)
    coverage_executor = getattr(runner, "coverage_executor", None)
    if coverage_executor is not None:
        await _await_named("coverage executor revocation", coverage_executor.shutdown)
        await _await_named("coverage executor join", coverage_executor.join)
    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        await _await_named("database executor revocation", db_executor.shutdown)
        await _await_named("database executor join", db_executor.join)
    database = getattr(runner, "database", None)
    if database is not None:
        await _await_named("database", database.close)
    await _await_named("telemetry", shutdown_telemetry)


__all__ = ["rollback_runner_resources", "rollback_runner_resources_async"]
