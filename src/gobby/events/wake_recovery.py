"""Replay durable mailbox wake intent at committed session boundaries."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, Protocol

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)

RunDb = Callable[..., Awaitable[Any]]


class WakeReplayDispatcher(Protocol):
    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]: ...


class WakeReplayCoordinator:
    """Own coalesced replay tasks for wake-marked durable messages."""

    def __init__(
        self,
        *,
        message_manager: InterSessionMessageManager,
        session_manager: SessionManager,
        dispatcher: WakeReplayDispatcher,
        run_db: RunDb,
    ) -> None:
        self._message_manager = message_manager
        self._dispatcher = dispatcher
        self._run_db = run_db
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._ready = False
        self._pending_recipients: set[str] = set()
        self._state_lock = threading.Lock()
        self._tasks: dict[str, asyncio.Task[dict[str, Any] | None]] = {}
        session_manager.register_status_transition_listener(self.on_status_transition)

    def bind_owner_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._owner_loop = loop

    def on_status_transition(self, transition: SessionStatusTransition) -> None:
        """Schedule replay only after a committed transition to paused."""
        if transition.status != "paused":
            return
        with self._state_lock:
            loop = self._owner_loop
            if not self._ready or loop is None or not loop.is_running() or loop.is_closed():
                self._pending_recipients.add(transition.session_id)
                return
        loop.call_soon_threadsafe(self._start_task, transition.session_id)

    async def open(self) -> None:
        """Open the startup gate and drain every durable replay candidate."""
        loop = asyncio.get_running_loop()
        if self._owner_loop is not loop:
            raise RuntimeError("Wake replay coordinator is not bound to the owner loop")
        stored = await self._run_db(self._message_manager.get_undelivered_wake_recipients)
        with self._state_lock:
            self._ready = True
            recipients = set(self._pending_recipients)
            self._pending_recipients.clear()
        recipients.update(str(session_id) for session_id in stored)
        tasks = [self._start_task(session_id) for session_id in sorted(recipients)]
        if tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )

    async def request_replay(self, session_id: str) -> dict[str, Any] | None:
        """Join one replay without allowing caller cancellation to cancel its owner."""
        with self._state_lock:
            if not self._ready:
                self._pending_recipients.add(session_id)
                return None
        task = self._start_task(session_id)
        return await asyncio.shield(task)

    def _start_task(self, session_id: str) -> asyncio.Task[dict[str, Any] | None]:
        existing = self._tasks.get(session_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(
            self._run_replay(session_id),
            name=f"wake-replay:{session_id}",
        )
        self._tasks[session_id] = task
        task.add_done_callback(partial(self._observe_task, session_id))
        return task

    async def _run_replay(self, session_id: str) -> dict[str, Any] | None:
        messages = await self._run_db(
            self._message_manager.get_undelivered_wake_messages,
            session_id,
        )
        if not messages:
            return None
        priority = (
            "urgent" if any(message.priority == "urgent" for message in messages) else "normal"
        )
        return await self._dispatcher.dispatch_live_wake(session_id, priority=priority)

    def _observe_task(
        self,
        session_id: str,
        task: asyncio.Task[dict[str, Any] | None],
    ) -> None:
        try:
            result = task.result()
        except asyncio.CancelledError:
            logger.warning("Wake replay task cancelled for session %s", session_id)
        except Exception as exc:
            logger.warning(
                "Wake replay failed for session %s",
                session_id,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        else:
            if result is None:
                logger.debug("Wake replay found no pending rows for session %s", session_id)
            elif result.get("delivered") is True:
                logger.debug("Wake replay dispatched for session %s", session_id)
            elif result.get("decline_reason"):
                logger.debug(
                    "Wake replay declined for session %s: %s",
                    session_id,
                    result["decline_reason"],
                )
            else:
                logger.warning(
                    "Wake replay did not dispatch for session %s: %s", session_id, result
                )
        finally:
            if self._tasks.get(session_id) is task:
                self._tasks.pop(session_id, None)
