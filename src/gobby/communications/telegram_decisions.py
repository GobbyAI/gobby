"""Durable settlement of Telegram decision-keyboard callbacks.

Callback tokens live only in the adapter's memory, so they lapse at their TTL and
vanish on daemon restart. The persisted outbound message stays the authority for a
decision. Its metadata carries ``callback_state`` (absent while pending, then
"answered" or "superseded") and ``callback_generation``, which names the keyboard
Telegram should show; every token carries the generation it was issued for and
the stored message that owns it.

Keyboard edits, reissues and acceptance of a decision run under one per-decision
lock. Each change is written to the database before it is published, so Telegram
can lag the stored decision but never lead it. Any click on a pending decision that
cannot be accepted at the stored generation republishes the stored text and
keyboard together, over every chunk of the message, which repairs a publish that
failed or went unconfirmed after its database write.

An answered decision whose comms-session answer failed, was interrupted or was
blocked (``storage.decision_answers``) shows that status under its text. Failed and
in-doubt answers also get one Retry answer button, published at a new generation;
its click consumes the attempt atomically and routes the next one, and a stale or
lost Retry button is reissued like a pending decision's keyboard.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from weakref import WeakValueDictionary

from gobby.communications.adapters.telegram import TelegramAdapter, TelegramEditNotApplied
from gobby.communications.models import CommsMessage

if TYPE_CHECKING:
    from gobby.communications.adapters.base import BaseChannelAdapter
    from gobby.communications.manager import CommunicationsManager
    from gobby.communications.models import ChannelConfig

logger = logging.getLogger(__name__)

# The trusted callback action of a Retry answer button; its value is the attempt it
# retries. Only the registry sets an action, so no ordinary option value can pose as one.
RETRY_ANSWER_ACTION = "retry_answer"
_RETRYABLE = frozenset({"failed", "in_doubt"})


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


def _sender_label(source: CommsMessage) -> str | None:
    label = source.metadata_json.get("telegram_sender_label")
    return label if isinstance(label, str) else None


async def _publish(
    manager: CommunicationsManager,
    adapter: TelegramAdapter,
    source: CommsMessage,
    content: str,
    chat_id: str,
    inline_keyboard: list[list[dict[str, str]]] | None,
    generation: int,
) -> None:
    """Rewrite every chunk of a stored message, recording new chunk IDs before any keyboard."""

    async def record(chunk_ids: list[str]) -> None:
        await asyncio.to_thread(manager._store.record_platform_message_ids, source.id, chunk_ids)

    await adapter.edit_stored_message(
        source,
        content,
        chat_id,
        sender_label=_sender_label(source),
        inline_keyboard=inline_keyboard,
        callback_generation=generation,
        record_chunk_ids=record,
    )


async def _republish_decision(
    manager: CommunicationsManager,
    adapter: TelegramAdapter,
    current: CommsMessage | None,
    message: CommsMessage,
) -> None:
    """Under the decision lock, answer a click that cannot be accepted.

    A closed decision reports its state. A pending one advances its stored
    generation and then republishes the stored text and keyboard with tokens for
    it; if the publish fails, the next click finds the generation mismatch and
    retries.
    """
    if current is None:
        message.metadata_json["callback_status"] = "invalid"
        return
    state = current.metadata_json.get("callback_state")
    if state == "answered" and await _reissue_answer_retry(manager, adapter, current):
        message.metadata_json["callback_status"] = "reissued"
        return
    if state is not None:
        message.metadata_json["callback_status"] = str(state)
        return
    generation = _generation(current.metadata_json)
    if not await asyncio.to_thread(manager._store.claim_callback_reissue, current.id, generation):
        message.metadata_json["callback_status"] = "retry"
        return
    try:
        await _publish(
            manager,
            adapter,
            current,
            current.content,
            str(message.metadata_json["chat_id"]),
            cast("list[list[dict[str, str]]]", current.metadata_json["inline_keyboard"]),
            generation + 1,
        )
    except Exception:
        logger.exception("Failed to republish Telegram decision %s", current.id)
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

    An ok click names its decision through its token, and is only tagged with
    ``callback_decision_id`` here; ``accept_decision_callback`` accepts it when it
    is persisted. Any other click on a decision is answered by
    ``_republish_decision`` through ``callback_status``.
    """
    status = message.metadata_json.get("callback_status")
    chat_id = message.metadata_json.get("chat_id")
    if not isinstance(adapter, TelegramAdapter) or not chat_id:
        return status == "ok"
    store = manager._store
    if status == "ok":
        source_id = message.metadata_json.get("callback_source_id")
        action = message.metadata_json.get("callback_action")
        if not source_id or (action and action != RETRY_ANSWER_ACTION):
            return True
        source = await asyncio.to_thread(store.get_message, str(source_id))
        # An unstored keyboard message (its outbound write failed) routes as before.
        if source is not None and _is_decision(source):
            message.metadata_json["callback_decision_id"] = source.id
        return True

    clicked_id = message.metadata_json.get("callback_source_message_id")
    source = (
        await asyncio.to_thread(
            store.get_message_by_platform_id,
            channel.name,
            str(clicked_id),
            platform_destination=str(chat_id),
        )
        if clicked_id
        else None
    )
    if source is None or not _is_decision(source):
        # Only a persisted decision in this chat can be reissued; any other stale click
        # is refused as invalid, even when the registry still recognized its token.
        message.metadata_json["callback_status"] = "invalid"
        return False
    async with manager.decision_locks(source.id):
        current = await asyncio.to_thread(store.get_message, source.id)
        await _republish_decision(manager, adapter, current, message)
    return False


async def accept_decision_callback(
    manager: CommunicationsManager,
    adapter: BaseChannelAdapter | None,
    message: CommsMessage,
) -> CommsMessage | None:
    """Persist an ok decision click as its answer, or None when the decision cannot take it.

    A Retry answer click is never persisted; it starts its answer's next attempt.
    """
    decision_id = str(message.metadata_json["callback_decision_id"])
    store = manager._store
    if message.metadata_json.get("callback_action") == RETRY_ANSWER_ACTION:
        retried = _retried_attempt(message)
        if retried is not None and isinstance(adapter, TelegramAdapter):
            await _retry_answer(manager, adapter, decision_id, retried, message)
        else:
            message.metadata_json["callback_status"] = "invalid"
        return None
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


async def publish_answer_status(
    manager: CommunicationsManager, adapter: TelegramAdapter, answer: CommsMessage
) -> None:
    """Show a comms-session answer's current delivery status on its decision message."""
    decision_id = answer.metadata_json.get("callback_decision_id")
    if not isinstance(decision_id, str):
        return
    async with manager.decision_locks(decision_id):
        await _publish_answer_status(manager, adapter, decision_id, answer.id)


async def _publish_answer_status(
    manager: CommunicationsManager, adapter: TelegramAdapter, decision_id: str, answer_id: str
) -> bool:
    """Under the decision lock, publish the answer's status at a new generation.

    Advancing the generation first refuses every older button, including a Retry
    answer for an attempt that already ran. True when the status was published.
    """
    ledger = manager.decision_answers
    decision = await asyncio.to_thread(manager._store.get_message, decision_id)
    answer = await asyncio.to_thread(ledger.get_answer, answer_id)
    if decision is None or answer is None:
        return False
    key = answer.answer_status_key
    chat_id = answer.metadata_json.get("chat_id")
    if key is None or not chat_id:
        return False
    generation = _generation(decision.metadata_json)
    if not await asyncio.to_thread(ledger.advance_answered_generation, decision_id, generation):
        return False
    keyboard = (
        [[{"text": "Retry answer", "value": str(answer.answer_attempt)}]]
        if answer.answer_outcome in _RETRYABLE
        else None
    )
    retry_source = replace(
        decision, metadata_json={**decision.metadata_json, "callback_action": RETRY_ANSWER_ACTION}
    )
    await _publish(
        manager,
        adapter,
        retry_source,
        f"{decision.content}\n\n{_status_text(answer)}",
        str(chat_id),
        keyboard,
        generation + 1,
    )
    await asyncio.to_thread(ledger.mark_status_shown, answer.id, key)
    return True


async def _reissue_answer_retry(
    manager: CommunicationsManager, adapter: TelegramAdapter, decision: CommsMessage
) -> bool:
    """Republish a lapsed or stale Retry answer button while its answer can still retry."""
    answer_id = decision.metadata_json.get("callback_answer_id")
    if not isinstance(answer_id, str):
        return False
    answer = await asyncio.to_thread(manager.decision_answers.get_answer, answer_id)
    if answer is None or answer.answer_outcome not in _RETRYABLE:
        return False
    try:
        return await _publish_answer_status(manager, adapter, decision.id, answer_id)
    except Exception:
        logger.exception("Failed to reissue the Retry answer button on decision %s", decision.id)
        return False


async def _retry_answer(
    manager: CommunicationsManager,
    adapter: TelegramAdapter,
    decision_id: str,
    attempt: int,
    message: CommsMessage,
) -> None:
    """Start the next attempt of a failed or in-doubt answer for one Retry answer click.

    The click already passed current channel access policy; the responder re-applies
    current delivery policy to the new attempt. A second click, or a click on a
    replaced button, loses the atomic consume and is answered like any stale click.
    """
    store = manager._store
    async with manager.decision_locks(decision_id):
        decision = await asyncio.to_thread(store.get_message, decision_id)
        answer_id = decision.metadata_json.get("callback_answer_id") if decision else None
        consumed = None
        if (
            decision is not None
            and isinstance(answer_id, str)
            and _generation(message.metadata_json) == _generation(decision.metadata_json)
        ):
            clicked_by = message.metadata_json.get("external_user_id") or message.identity_id
            consumed = await asyncio.to_thread(
                manager.decision_answers.consume_retry, answer_id, attempt, str(clicked_by or "")
            )
        if consumed is None:
            await _republish_decision(manager, adapter, decision, message)
            return
        message.metadata_json["callback_status"] = "retrying"
        try:
            await _publish_answer_status(manager, adapter, decision_id, consumed.id)
        except Exception:
            logger.exception("Failed to show the retry of decision %s", decision_id)
    await manager.responder.handle_message(consumed)


def _retried_attempt(message: CommsMessage) -> int | None:
    value = message.metadata_json.get("callback_value")
    return int(value) if isinstance(value, str) and value.isdecimal() else None


def _status_text(answer: CommsMessage) -> str:
    value = answer.content
    attempt = answer.answer_attempt
    rerun = "Retry answer runs it once more and may repeat actions that already happened."
    match answer.answer_outcome:
        case "failed":
            return (
                f"Answer “{value}” was recorded, but its turn failed and may have partly "
                f"acted. {rerun}"
            )
        case "in_doubt":
            return (
                f"Answer “{value}” was recorded, but its turn was interrupted before it "
                f"finished and may have partly acted. {rerun}"
            )
        case "blocked":
            return f"Answer “{value}” was recorded but not delivered: this chat's access changed."
        case "pending":
            return f"Retrying answer “{value}” (attempt {attempt})."
        case _:
            return f"Answer “{value}” delivered on retry (attempt {attempt})."


async def edit_keyboard_message(
    manager: CommunicationsManager,
    telegram: TelegramAdapter,
    message_id: str,
    content: str,
    conversation_id: str,
    inline_keyboard: list[list[dict[str, str]]] | None,
) -> None:
    """Edit a stored keyboard message, staging its new generation before publishing.

    The staged row is restored only when the edit definitely changed no existing
    chunk. Any other failure leaves it staged, because Telegram may show part of
    the edit; old buttons are refused at the staged generation, and the next click
    republishes the staged text and keyboard together.
    """
    store = manager._store
    async with manager.decision_locks(message_id):
        current = await asyncio.to_thread(store.get_message, message_id)
        if current is None:
            raise ValueError("Cannot edit a Telegram keyboard without its stored message")
        state = current.metadata_json.get("callback_state")
        if inline_keyboard is not None and state is not None:
            raise ValueError(
                f"Decision {message_id} is {state}; send a new decision instead of new buttons"
            )
        generation = _generation(current.metadata_json)
        if not await asyncio.to_thread(
            store.stage_callback_edit, message_id, generation, content, inline_keyboard
        ):
            raise RuntimeError(f"Decision {message_id} changed during a locked edit")
        try:
            await _publish(
                manager,
                telegram,
                current,
                content,
                conversation_id,
                inline_keyboard,
                generation + 1,
            )
        except TelegramEditNotApplied:
            if not await asyncio.to_thread(store.restore_callback_edit, current, generation + 1):
                logger.error("Could not restore decision %s after a refused edit", message_id)
            raise
