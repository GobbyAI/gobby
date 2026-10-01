"""Shared wake dispatcher test helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime

from gobby.events.wake import WakeDispatcher


class PendingWakeLedger:
    """In-memory stand-in for the durable unread-wake check on mocked managers.

    A recorded wake stays pending for the rest of the test, as if its messages
    stay unread; tests/events/test_wake_unread_dedup.py covers the real store.
    """

    def __init__(self, dispatcher: WakeDispatcher) -> None:
        self.recorded: list[str] = []
        vars(dispatcher).update(
            _should_send_live_wake=self._should_send,
            _record_live_wake=self._record,
        )

    async def _should_send(self, session_id: str) -> bool:
        await asyncio.sleep(0)
        return session_id not in self.recorded

    async def _record(self, session_id: str, attempted_at: datetime) -> None:
        self.recorded.append(session_id)
