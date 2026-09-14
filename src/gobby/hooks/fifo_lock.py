"""FIFO mutex shared by coroutines running on different event loops."""

from __future__ import annotations

import asyncio
import threading
from collections import deque


class CrossLoopFifoLock:
    """Mutex granted in arrival order to coroutines on any event loop.

    ``release`` may run on any thread. It hands ownership straight to the
    oldest waiter, so a newcomer never barges past queued waiters, and a
    waiter cancelled after ownership reached it passes ownership on.
    """

    def __init__(self) -> None:
        self._mutex = threading.Lock()
        self._locked = False
        self._waiters: deque[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = deque()

    def locked(self) -> bool:
        return self._locked

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        with self._mutex:
            if not self._locked:
                self._locked = True
                return
            waiter: asyncio.Future[None] = loop.create_future()
            self._waiters.append((loop, waiter))
        try:
            await waiter
        except BaseException:
            with self._mutex:
                queued = (loop, waiter) in self._waiters
                if queued:
                    self._waiters.remove((loop, waiter))
            if not queued and waiter.done() and not waiter.cancelled():
                # Ownership arrived before the cancellation did.
                self.release()
            raise

    def release(self) -> None:
        with self._mutex:
            if not self._locked:
                raise RuntimeError("CrossLoopFifoLock is not acquired")
            if not self._waiters:
                self._locked = False
                return
            loop, waiter = self._waiters.popleft()
            loop.call_soon_threadsafe(self._grant, waiter)

    def _grant(self, waiter: asyncio.Future[None]) -> None:
        # A waiter cancelled while ownership was in transit passes it on.
        if waiter.cancelled():
            self.release()
        else:
            waiter.set_result(None)
