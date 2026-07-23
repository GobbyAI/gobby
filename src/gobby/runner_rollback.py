"""Construction rollback ledger for partially initialized daemon runners."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class ConstructionRollbackLedger:
    """Run registered construction compensations once, in reverse order."""

    def __init__(self) -> None:
        self._callbacks: list[tuple[str, Callable[[], None]]] = []
        self._committed = False

    def add(self, name: str, callback: Callable[[], None]) -> None:
        self._callbacks.append((name, callback))

    def commit(self) -> None:
        self._committed = True
        self._callbacks.clear()

    def rollback(self) -> None:
        if self._committed:
            return
        for name, callback in reversed(self._callbacks):
            try:
                callback()
            except BaseException:
                logger.warning("Runner construction rollback failed for %s", name, exc_info=True)
        self._callbacks.clear()


def _settle_async_close(awaitable: Coroutine[Any, Any, Any]) -> None:
    """Settle an async close from a synchronous constructor rollback."""

    async def settle_with_timeout() -> None:
        await asyncio.wait_for(awaitable, timeout=5.0)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(settle_with_timeout())
        return

    error: list[BaseException] = []

    def settle() -> None:
        try:
            asyncio.run(settle_with_timeout())
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=settle, name="gobby-init-rollback", daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    if thread.is_alive():
        raise TimeoutError("Async construction rollback did not settle")
    if error:
        raise error[0]


def rollback_runner_resources(runner: Any) -> None:
    """Release every known daemon-owned side effect installed during construction."""
    from gobby.agents.agent_cleanup import reset_terminal_delivery_offload
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
        ("web chat runtime", getattr(http_services, "web_chat_runtime_manager", None)),
        ("memory manager", getattr(runner, "memory_manager", None)),
        ("vector store", getattr(runner, "vector_store", None)),
    )
    for name, resource in resources:
        close = getattr(resource, "close", None) or getattr(resource, "stop", None)
        if callable(close):
            settle(name, close)

    db_executor = getattr(runner, "db_executor", None)
    if db_executor is not None:
        settle("database executor revocation", db_executor.shutdown)
        settle("database executor join", db_executor.join)
    database = getattr(runner, "database", None)
    if database is not None:
        settle("database", database.close)
    settle("telemetry", shutdown_telemetry)


__all__ = ["ConstructionRollbackLedger", "rollback_runner_resources"]
