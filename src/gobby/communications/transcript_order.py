"""Strict in-order delivery for the text blocks of one mirrored transcript turn."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from gobby.communications.models import CommsMessage
from gobby.communications.outbound import transcript_delivery_id
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager

MAX_DELIVERY_ATTEMPTS = 3


async def hold_transcript_message(
    manager: CommunicationsManager,
    session_id: str,
    message_id: str,
    content: str,
    metadata: dict[str, Any],
) -> None:
    """Reserve a block of a turn as failed, without sending it.

    The retry loop sends it once the block before it is delivered. An existing
    row is left alone: reserving it again would reclaim it without a send.
    """
    for channel in tuple(manager._channel_by_name.values()):
        if channel.channel_type != "telegram" or not channel.enabled:
            continue
        if manager.attached_destination(channel.id, session_id) is None:
            continue
        if await asyncio.to_thread(manager.get_transcript_delivery, session_id, message_id):
            continue
        held = await manager._outbound.enrich_metadata(channel, channel.name, session_id, metadata)
        held.update(
            transcript_message_id=message_id,
            transcript_delivery_attempt=0,
            transcript_lease_until=0.0,
            transcript_retry_at=utc_now().timestamp(),
        )
        thread_id = manager._get_thread_id(channel.id, session_id)
        if "thread_id" in held:
            explicit = held["thread_id"]
            thread_id = explicit.strip() or None if isinstance(explicit, str) else None
        await asyncio.to_thread(
            manager._store.reserve_transcript_message,
            CommsMessage(
                id=transcript_delivery_id(session_id, message_id),
                channel_id=channel.id,
                direction="outbound",
                content=content,
                session_id=session_id,
                status="failed",
                error="Held behind an undelivered block of its turn",
                platform_thread_id=thread_id,
                metadata_json=held,
                created_at=utc_now(),
            ),
        )


def waits_for_earlier_block(
    manager: CommunicationsManager, session_id: str, message: CommsMessage
) -> bool:
    """Whether the block before this one in its turn may still be delivered first.

    A missing earlier row waits: the earlier block was never recorded, so
    sending this one would put it out of order.
    """
    turn = message.metadata_json.get("transcript_turn_id")
    index = message.metadata_json.get("transcript_block_index")
    if not isinstance(turn, str) or not isinstance(index, int) or index <= 0:
        return False
    earlier = manager.get_transcript_delivery(session_id, f"{turn}#{index - 1}")
    if earlier is None:
        return True
    if earlier.status not in {"pending", "failed"}:
        return False
    lease = float(earlier.metadata_json.get("transcript_lease_until") or 0)
    if earlier.status == "pending" and lease >= utc_now().timestamp():
        return True
    # An exhausted earlier block will never arrive; send the rest rather than lose it.
    attempts = int(earlier.metadata_json.get("transcript_delivery_attempt") or 0)
    return attempts < MAX_DELIVERY_ATTEMPTS
