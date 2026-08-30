"""Webhook and MCP dispatch plus dispatcher shutdown for HookManager."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Any

from gobby.hooks.dispatchers import mcp as mcp_dispatcher
from gobby.hooks.dispatchers import webhook as webhook_dispatcher
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import HookEvent, HookResponse


class HookManagerDispatchMixin:
    """Dispatch transport and dispatcher-close helpers owned by HookManager."""

    _webhook_dispatcher: Any
    logger: logging.Logger
    _loop: asyncio.AbstractEventLoop | None
    tool_proxy_getter: Any | None

    def _evaluate_blocking_webhooks(
        self,
        event: HookEvent,
        blocking_deadline: BlockingEffectDeadline | None = None,
    ) -> HookResponse | None:
        """Evaluate blocking webhooks before handler execution."""
        return webhook_dispatcher.evaluate_blocking_webhooks(
            event,
            self._webhook_dispatcher,
            self.logger,
            self._loop,
            deadline=blocking_deadline,
        )

    def _dispatch_webhooks_sync(self, event: HookEvent, blocking_only: bool = False) -> list[Any]:
        """Dispatch webhooks synchronously (for blocking webhooks)."""
        return webhook_dispatcher.dispatch_webhooks_sync(
            event, self._webhook_dispatcher, self.logger, blocking_only
        )

    def _dispatch_webhooks_async(
        self, event: HookEvent, response: HookResponse | None = None
    ) -> None:
        """Dispatch non-blocking webhooks asynchronously (fire-and-forget)."""
        webhook_dispatcher.dispatch_webhooks_async(
            event, self._webhook_dispatcher, self.logger, self._loop, response
        )

    def _dispatch_mcp_calls(
        self,
        mcp_calls: list[dict[str, Any]],
        event: HookEvent,
        *,
        deadline: BlockingEffectDeadline | None = None,
    ) -> list[dict[str, Any]]:
        """Dispatch mcp_call effects from rule engine evaluation."""
        return mcp_dispatcher.dispatch_mcp_calls(
            mcp_calls,
            event,
            self.tool_proxy_getter,
            self._loop,
            self.logger,
            deadline=deadline,
        )

    def _run_coro_blocking(
        self,
        coro: Any,
        *,
        label: str | None = None,
        timeout_seconds: float = 30,
    ) -> Any:
        """Run a coroutine blocking, using the best available event loop strategy."""
        return mcp_dispatcher.run_coro_blocking(
            coro,
            self._loop,
            self.logger,
            label=label,
            timeout_seconds=timeout_seconds,
        )

    async def _close_webhook_dispatcher_async(self) -> None:
        try:
            await self._webhook_dispatcher.close()
        except Exception as exc:
            self._log_webhook_dispatcher_close_failure(exc)

    def _close_webhook_dispatcher_sync(self) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None:
            running_loop.create_task(self._close_webhook_dispatcher_async())
            self.logger.debug("Scheduled webhook dispatcher close on current event loop")
            return

        try:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self._close_webhook_dispatcher_async(), self._loop
                ).result(timeout=5.0)
            else:
                asyncio.run(self._close_webhook_dispatcher_async())
        except concurrent.futures.TimeoutError:
            self.logger.warning(
                "Timed out closing webhook dispatcher after 5.0s",
                exc_info=True,
            )
        except Exception as exc:
            self._log_webhook_dispatcher_close_failure(exc)

    def _log_webhook_dispatcher_close_failure(self, exc: Exception) -> None:
        message = str(exc) or "<no message>"
        self.logger.warning(
            "Failed to close webhook dispatcher (%s): %s",
            type(exc).__name__,
            message,
            exc_info=True,
        )
