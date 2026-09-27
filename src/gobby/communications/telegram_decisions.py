"""Durable settlement of Telegram decision-keyboard callbacks.

Callback tokens live only in the adapter's memory, so they lapse at their TTL and
vanish on daemon restart. The persisted outbound message stays the authority for a
decision. Its metadata carries ``callback_state`` (absent while pending, then
"answered" or "superseded") and ``callback_generation``, which names the keyboard
Telegram should show; every token carries the generation it was issued for.

Keyboard edits, reissues and acceptance of a decision run under one per-decision
lock. Each change is written to the database before it is published, so Telegram
can lag the stored decision but never lead it. Any click on a pending decision that
cannot be accepted at the stored generation publishes the stored keyboard afresh,
which repairs a publish that failed after its database write.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

import httpx

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.models import CommsMessage

if TYPE_CHECKING:
    from gobby.communications.adapters.base import BaseChannelAdapter
    from gobby.communications.manager import CommunicationsManager
    from gobby.communications.models import ChannelConfig

logger = logging.getLogger(__name__)


class DecisionLocks:
    """Per-decision asyncio locks, released once no caller holds them."""

    def __init__(self) -> None:
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def __call__(self, decision_id: str) -> asyncio.Lock:
        lock = self._locks.get(decision_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[decision_id] = lock
        return lock


def _generation(metadata: dict[str, object]) -> int:
    value = metadata.get("callback_generation")
    return value if isinstance(value, int) else 0


def _is_decision(source: CommsMessage) -> bool:
    # Action keyboards (agent menus) are re-opened by command, not reissued.
    return (
        source.direction == "outbound"
        and bool(source.session_id)
        and bool(source.metadata_json.get("inline_keyboard"))
        and not source.metadata_json.get("callback_action")
    )


async def _republish_decision(
    manager: CommunicationsManager,
    adapter: TelegramAdapter,
    current: CommsMessage | None,
    message: CommsMessage,
) -> None:
    """Under the decision lock, answer a click that cannot be accepted.

    A closed decision reports its state. A pending one advances its stored
    generation and then publishes the stored keyboard with tokens for it; if the
    publish fails, the next click finds the generation mismatch and retries.
    """
    if current is None:
        message.metadata_json["callback_status"] = "invalid"
        return
    state = current.metadata_json.get("callback_state")
    if state is not None:
        message.metadata_json["callback_status"] = str(state)
        return
    generation = _generation(current.metadata_json)
    if not await asyncio.to_thread(manager._store.claim_callback_reissue, current.id, generation):
        message.metadata_json["callback_status"] = "retry"
        return
    try:
        await adapter.reissue_callback_keyboard(
            current,
            str(message.metadata_json["chat_id"]),
            str(message.metadata_json["callback_source_message_id"]),
            generation=generation + 1,
        )
    except Exception:
        logger.exception("Failed to reissue Telegram decision buttons for %s", current.id)
        message.metadata_json["callback_status"] = "retry"
        return
    message.metadata_json["callback_status"] = "reissued"


async def settle_decision_callback(
    manager: CommunicationsManager,
    channel: ChannelConfig,
    adapter: BaseChannelAdapter | None,
    message: CommsMessage,
) -> bool:
    """Classify a callback against durable decision state; True when it should be routed.

    An ok click on a decision is only tagged with ``callback_decision_id`` here;
    ``accept_decision_callback`` accepts it when it is persisted. Any other click on
    a decision is answered by ``_republish_decision`` through ``callback_status``.
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
        message.metadata_json["callback_decision_id"] = source.id
        return True
    async with manager.decision_locks(source.id):
        current = await asyncio.to_thread(store.get_message, source.id)
        await _republish_decision(manager, adapter, current, message)
    return False


async def accept_decision_callback(
    manager: CommunicationsManager,
    adapter: BaseChannelAdapter | None,
    message: CommsMessage,
) -> CommsMessage | None:
    """Persist an ok decision click as its answer, or None when the decision cannot take it."""
    decision_id = str(message.metadata_json["callback_decision_id"])
    store = manager._store
    async with manager.decision_locks(decision_id):
        accepted = await asyncio.to_thread(
            store.accept_callback_decision,
            decision_id,
            _generation(message.metadata_json),
            message,
        )
        if accepted is None:
            current = await asyncio.to_thread(store.get_message, decision_id)
            if isinstance(adapter, TelegramAdapter):
                await _republish_decision(manager, adapter, current, message)
            else:
                message.metadata_json["callback_status"] = "invalid"
    return accepted


async def edit_keyboard_message(
    manager: CommunicationsManager,
    telegram: TelegramAdapter,
    message_id: str,
    platform_message_id: str,
    content: str,
    conversation_id: str,
    sender_label: str | None,
    inline_keyboard: list[list[dict[str, str]]] | None,
) -> None:
    """Edit a stored keyboard message, staging its new generation before publishing.

    When Telegram refuses the edit, the staged row is restored. A transport failure
    leaves it staged, because Telegram may have applied the edit.
    """
    store = manager._store
    async with manager.decision_locks(message_id):
        current = await asyncio.to_thread(store.get_message, message_id)
        if current is None:
            raise ValueError("Cannot edit a Telegram keyboard without its stored message")
        generation = _generation(current.metadata_json)
        if not await asyncio.to_thread(
            store.stage_callback_edit, message_id, generation, content, inline_keyboard
        ):
            raise RuntimeError(f"Decision {message_id} changed during a locked edit")
        try:
            await telegram.edit_message(
                platform_message_id,
                content,
                conversation_id,
                sender_label=sender_label,
                inline_keyboard=inline_keyboard,
                callback_source=current if inline_keyboard is not None else None,
                callback_generation=generation + 1,
            )
        except (RuntimeError, httpx.HTTPStatusError):
            if not await asyncio.to_thread(store.restore_callback_edit, current, generation + 1):
                logger.error("Could not restore decision %s after a refused edit", message_id)
            raise
