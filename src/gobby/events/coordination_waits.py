"""Event-driven coordination delivery using the existing completion wake contract."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.config_notifications import NotificationConnection
from gobby.storage.coordination_waits import CoordinationWaitManager, coordination_wait_payload
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class CoordinationWaitService:
    def __init__(
        self,
        manager: CoordinationWaitManager,
        registry: CompletionEventRegistry,
        machine_id: str,
        connection_factory: Callable[[], Awaitable[NotificationConnection]],
    ) -> None:
        self.manager = manager
        self.registry = registry
        self.machine_id = machine_id
        self.connection_factory = connection_factory
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._listener: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if self._listener is not None:
            return
        self._worker = asyncio.create_task(self._consume(), name="coordination-delivery")
        self._listener = asyncio.create_task(self._listen(), name="coordination-listener")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for task in (self._listener, self._worker):
            if task is not None:
                task.cancel()
        for task in (self._listener, self._worker):
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        self._listener = self._worker = None
        self._ready.clear()

    async def recover(self) -> None:
        for wait_id in await asyncio.to_thread(self.manager.pending_ids, self.machine_id):
            self._queue.put_nowait(wait_id)

    async def _listen(self) -> None:
        while True:
            connection = None
            try:
                connection = await self.connection_factory()
                await connection.execute("LISTEN gobby_coordination_wait")
                # LISTEN before snapshot: committed events during recovery queue
                # on the socket. Every reconnect repeats this durable recovery.
                await self.recover()
                self._ready.set()
                async for notification in connection.notifies():
                    self._queue.put_nowait(notification.payload)
                raise ConnectionError("Coordination notification stream ended")
            except Exception:
                logger.exception("Coordination listener disconnected; recovering subscriptions")
            finally:
                if connection is not None:
                    try:
                        await connection.close()
                    except Exception:
                        logger.exception("Could not close disconnected coordination listener")
            # Transport reconnection backoff, never a condition/status poll.
            await asyncio.sleep(1)

    def _schedule(self, wait_id: str, delay: float) -> None:
        previous = self._timers.pop(wait_id, None)
        if previous is not None:
            previous.cancel()
        self._timers[wait_id] = asyncio.get_running_loop().call_later(
            max(0, delay), self._queue.put_nowait, wait_id
        )

    async def _consume(self) -> None:
        while True:
            wait_id = await self._queue.get()
            try:
                await self.process(wait_id)
            except Exception:
                logger.exception("Coordination delivery failed for %s; retaining outcome", wait_id)
                self._schedule(wait_id, 5)
            finally:
                self._queue.task_done()

    async def process(self, wait_id: str) -> None:
        """Resolve one notified/deadline wait; the single consumer serializes retries."""
        previous = self._timers.pop(wait_id, None)
        if previous is not None:
            previous.cancel()
        row = await asyncio.to_thread(self.manager.refresh, wait_id, self.machine_id)
        if row is None or row["delivered_at"] is not None:
            return
        if row["outcome"] == "waiting":
            self._schedule(wait_id, (row["expires_at"] - utc_now()).total_seconds())
            return
        waiter = str(row["waiter_session_id"])
        delivery = await self.registry.wake_sessions(
            wait_id,
            [waiter],
            coordination_wait_payload(row),
            message=f"Coordination wait {wait_id}: {row['outcome']}",
        )
        if delivery.get(waiter):
            await asyncio.to_thread(self.manager.mark_delivered, wait_id)
        else:
            self._schedule(wait_id, 5)
