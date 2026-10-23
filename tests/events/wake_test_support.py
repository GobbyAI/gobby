"""Shared wake dispatcher test helpers."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from gobby.events.wake import WakeDispatcher
from gobby.storage.inter_session_messages import InterSessionMessage
from gobby.storage.sessions import system_session_id


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


def operator_comms_message(session_id: str) -> InterSessionMessage:
    """Daemon-ingress operator DM: the only pending wake that may wipe a composer."""
    return InterSessionMessage(
        id=f"operator-dm-{session_id}",
        from_session=system_session_id(),
        to_session=session_id,
        content="Status?",
        priority="normal",
        sent_at=datetime.now(UTC),
        message_type="comms_message",
        metadata_json=json.dumps(
            {
                "communications_message_id": "inbound-dm",
                "operator_comms": True,
                "wake_requested": True,
                "channel": "telegram",
            }
        ),
    )


def plain_message(session_id: str) -> InterSessionMessage:
    """An ordinary agent message, which never enters the focus hold."""
    return InterSessionMessage(
        id=f"plain-{session_id}",
        from_session=system_session_id(),
        to_session=session_id,
        content="Heads up",
        priority="normal",
        sent_at=datetime.now(UTC),
        message_type="message",
        metadata_json=None,
    )
