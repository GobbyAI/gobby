"""Durable settlement of Telegram decision-keyboard callbacks.

Callback tokens live only in the adapter's memory, so they lapse at their TTL and
vanish on daemon restart. The persisted outbound message stays the authority for a
decision: a stale click on a still-pending decision reissues fresh buttons scoped
from that stored message, and a decision is delivered to its session at most once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.models import CommsMessage

if TYPE_CHECKING:
    from gobby.communications.adapters.base import BaseChannelAdapter
    from gobby.communications.manager import CommunicationsManager
    from gobby.communications.models import ChannelConfig

logger = logging.getLogger(__name__)


def _is_decision(source: CommsMessage | None) -> bool:
    # Action keyboards (agent menus) are re-opened by command, not reissued.
    return (
        source is not None
        and source.direction == "outbound"
        and bool(source.session_id)
        and bool(source.metadata_json.get("inline_keyboard"))
        and not source.metadata_json.get("callback_action")
    )


async def settle_decision_callback(
    manager: CommunicationsManager,
    channel: ChannelConfig,
    adapter: BaseChannelAdapter | None,
    message: CommsMessage,
) -> bool:
    """Apply durable decision state to a callback; return True when it should be routed.

    Updates ``callback_status`` to ``answered``, ``superseded`` or ``reissued`` when
    the durable state decides the outcome, so the callback answer tells the user.
    """
    status = message.metadata_json.get("callback_status")
    source_id = message.metadata_json.get("callback_source_message_id")
    chat_id = message.metadata_json.get("chat_id")
    if not isinstance(adapter, TelegramAdapter) or not source_id or not chat_id:
        return status == "ok"
    store = manager._store
    source = await asyncio.to_thread(
        store.get_message_by_platform_id,
        channel.name,
        str(source_id),
        platform_destination=str(chat_id),
    )
    if source is None or not _is_decision(source):
        return status == "ok"

    if status == "ok":
        if await asyncio.to_thread(store.answer_callback_decision, source.id):
            return True
        await _mark_closed(manager, message, source.id)
        return False

    closed_state = source.metadata_json.get("callback_state")
    if closed_state is not None:
        message.metadata_json["callback_status"] = str(closed_state)
        return False
    generation = int(source.metadata_json.get("callback_generation") or 0)
    if not await asyncio.to_thread(store.claim_callback_reissue, source.id, generation):
        # A concurrent click already reissued, or the decision closed meanwhile.
        await _mark_closed(manager, message, source.id, default="reissued")
        return False
    try:
        await adapter.reissue_callback_keyboard(source, str(chat_id), str(source_id))
    except Exception:
        logger.exception("Failed to reissue Telegram decision buttons for %s", source.id)
        return False
    message.metadata_json["callback_status"] = "reissued"
    return False


async def _mark_closed(
    manager: CommunicationsManager,
    message: CommsMessage,
    source_id: str,
    *,
    default: str = "answered",
) -> None:
    current = await asyncio.to_thread(manager._store.get_message, source_id)
    state = current.metadata_json.get("callback_state") if current is not None else None
    message.metadata_json["callback_status"] = str(state) if state else default
