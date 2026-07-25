"""Deferred daemon memory recall scheduling and delivery."""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from gobby.hooks.events import HookEvent
from gobby.hooks.memory_recall_delivery import MemoryRecallDeliveryQueue

if TYPE_CHECKING:
    from gobby.llm.service import LLMService
    from gobby.memory.manager import MemoryManager
    from gobby.storage.hub.protocol import HubDatabase


class MemoryRecallDispatcher:
    """Own deferred recall scheduling, delivery, deduplication, and shutdown."""

    def __init__(
        self,
        *,
        config: Any,
        database: HubDatabase | None,
        memory_manager: MemoryManager | None,
        llm_service: LLMService | None,
        loop: asyncio.AbstractEventLoop | None,
        logger: logging.Logger,
    ) -> None:
        self._config = config
        self._database = database
        self._memory_manager = memory_manager
        self._llm_service = llm_service
        self._delivery_queue = MemoryRecallDeliveryQueue(database) if database else None
        self._loop = loop
        self._logger = logger
        self._tasks: dict[tuple[str, int], concurrent.futures.Future[Any]] = {}
        self._lock = threading.Lock()
        self._closing = False

    def schedule(self, event: HookEvent) -> None:
        """Schedule daemon-owned recall for deferred reference delivery."""
        session_id = event.metadata.get("_platform_session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        config = getattr(self._config, "memory_recall", None)
        if config is None or self._memory_manager is None or self._database is None:
            return

        try:
            from gobby.workflows.state_manager import SessionVariableManager

            variables = SessionVariableManager(self._database).get_variables(session_id)
            parent_turn_seq = variables.get("parent_turn_seq")
            if not isinstance(parent_turn_seq, int) or isinstance(parent_turn_seq, bool):
                return

            key = (session_id, parent_turn_seq)
            event_snapshot = copy.deepcopy(event)
            with self._lock:
                if self._closing:
                    self._logger.debug("Skipping deferred memory recall during shutdown")
                    return
                self._prune_tasks(session_id, parent_turn_seq)
                if key in self._tasks:
                    self._logger.debug(
                        "Memory recall already scheduled for session=%s parent_turn_seq=%s",
                        session_id,
                        parent_turn_seq,
                    )
                    return

                future = self._schedule_task(
                    key,
                    self._run_deferred_recall(
                        event_snapshot,
                        session_id,
                        dict(variables),
                    ),
                )
                if future is not None:
                    self._tasks[key] = future
        except Exception as exc:  # noqa: BLE001 - recall must fail open at hook boundary
            self._logger.warning("Daemon memory recall scheduling failed: %s", exc)

    def shutdown(self) -> None:
        """Cancel and drain deferred recall work from a synchronous context."""
        items = self._take_tasks_for_shutdown()
        if not items:
            return

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not None and running_loop.is_running():
            for key, future in items:
                wrapped = asyncio.wrap_future(future)

                def log_done(
                    done: asyncio.Future[Any],
                    *,
                    recall_key: tuple[str, int] = key,
                ) -> None:
                    self._log_task_result(recall_key, done)

                wrapped.add_done_callback(log_done)
            return

        deadline = time.monotonic() + 5.0
        for key, future in items:
            try:
                remaining = max(0.0, deadline - time.monotonic())
                future.result(timeout=remaining)
            except concurrent.futures.TimeoutError:
                self._logger.warning("Timed out cancelling deferred memory recall: %s", key)
            except (asyncio.CancelledError, concurrent.futures.CancelledError):
                self._logger.debug("Deferred memory recall cancelled: %s", key)
            except Exception as exc:  # noqa: BLE001 - shutdown should continue
                self._logger.warning("Deferred memory recall failed during shutdown: %s", exc)

    async def shutdown_async(self) -> None:
        """Cancel and drain deferred recall work from an asynchronous context."""
        items = self._take_tasks_for_shutdown()
        if not items:
            return
        futures = [asyncio.wrap_future(future) for _key, future in items]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*futures, return_exceptions=True),
                timeout=5.0,
            )
        except TimeoutError:
            self._logger.warning("Timed out cancelling deferred memory recall tasks")
            return
        for result in results:
            if isinstance(result, (asyncio.CancelledError, concurrent.futures.CancelledError)):
                continue
            if isinstance(result, Exception):
                self._logger.warning("Deferred memory recall failed during shutdown: %s", result)

    def _prune_tasks(self, session_id: str, parent_turn_seq: int) -> None:
        for key, future in list(self._tasks.items()):
            key_session_id, key_turn_seq = key
            if key_session_id == session_id and key_turn_seq < parent_turn_seq and future.done():
                del self._tasks[key]

    def _schedule_task(
        self,
        key: tuple[str, int],
        coro: Any,
    ) -> concurrent.futures.Future[Any] | None:
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            self._logger.debug(
                "Skipping deferred memory recall scheduling without a running event loop: %s",
                key,
            )
            return None

        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            self._logger.debug("Skipping deferred memory recall scheduling; loop unavailable")
            return None

        future.add_done_callback(lambda done: self._log_task_result(key, done))
        return future

    def _log_task_result(
        self,
        key: tuple[str, int],
        future: concurrent.futures.Future[Any] | asyncio.Future[Any],
    ) -> None:
        try:
            future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            self._logger.debug("Deferred memory recall cancelled: %s", key)
        except Exception as exc:  # noqa: BLE001 - background recall must fail open
            self._logger.warning("Deferred memory recall failed for %s: %s", key, exc)

    async def _run_deferred_recall(
        self,
        event: HookEvent,
        session_id: str,
        variables: dict[str, Any],
    ) -> None:
        try:
            from gobby.memory.recall import MemoryRecallRunner

            config = getattr(self._config, "memory_recall", None)
            if (
                config is None
                or self._memory_manager is None
                or self._database is None
                or self._delivery_queue is None
            ):
                return
            runner = MemoryRecallRunner(
                db=self._database,
                memory_manager=self._memory_manager,
                llm_service=self._llm_service,
                config=config,
                log=self._logger,
            )
            result = await runner.run(event, session_id, variables, require_same_turn=False)
            if result is None or not result.memories:
                return

            self._delivery_queue.queue(
                session_id,
                recall_request_id=result.recall_request_id,
                origin_turn_seq=result.origin_turn_seq,
                project_id=event.project_id,
                memories=result.memories,
            )
        except Exception as exc:  # noqa: BLE001 - recall must fail open at hook boundary
            self._logger.warning("Deferred daemon memory recall failed: %s", exc)

    def _take_tasks_for_shutdown(
        self,
    ) -> list[tuple[tuple[str, int], concurrent.futures.Future[Any]]]:
        with self._lock:
            self._closing = True
            items = list(self._tasks.items())
            self._tasks.clear()
        for _key, future in items:
            if not future.done():
                future.cancel()
        return items
