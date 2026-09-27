"""Durable Telegram decision-keyboard settlement against the real communications store."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.identities import IdentityManager, IdentityResolution
from gobby.communications.inbound import InboundCommunications
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.communications.telegram_callbacks import TelegramCallbackRegistry
from gobby.communications.telegram_decisions import DecisionLocks, edit_keyboard_message
from gobby.config.communications import CommunicationsConfig
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.integration

_PROJECT_ID = "00000000-0000-0000-0000-000000000000"
_CHAT_ID = "2222222"
_SOURCE_MESSAGE_ID = 99
_TS = datetime(2026, 9, 27, tzinfo=UTC)
_KEYBOARD = [[{"text": "Approve", "value": "approve"}, {"text": "Changes", "value": "changes"}]]
_OK: dict[str, Any] = {"ok": True, "result": True}


@dataclass
class _Decision:
    store: LocalCommunicationsStore
    channel: ChannelConfig
    identity: CommsIdentity
    session_id: str
    source: CommsMessage


@pytest.fixture
def decision(temp_db: HubDatabase) -> _Decision:
    store = LocalCommunicationsStore(temp_db, project_id=_PROJECT_ID)
    channel = store.create_channel(
        ChannelConfig(
            id="dddddddd-1111-4ddd-8ddd-dddddddd0001",
            channel_type="telegram",
            name="telegram",
            enabled=True,
            config_json={},
            created_at=_TS,
            updated_at=_TS,
        )
    )
    identity = IdentityManager(
        store, SessionManager(temp_db), CommunicationsConfig(auto_create_sessions=True)
    ).resolve_identity(
        channel_id=channel.id,
        external_user_id="1111111",
        external_username="josh",
        metadata={},
        project_id=_PROJECT_ID,
    )
    assert identity.session_id is not None
    source = store.create_message(
        CommsMessage(
            id="",
            channel_id=channel.id,
            direction="outbound",
            content="Approve the design?",
            platform_message_id=str(_SOURCE_MESSAGE_ID),
            session_id=identity.session_id,
            metadata_json={
                "platform_destination": _CHAT_ID,
                "inline_keyboard": _KEYBOARD,
                "callback_ttl_seconds": 30,
            },
            created_at=_TS,
        )
    )
    return _Decision(store, channel, identity, identity.session_id, source)


class _RecordingAdapter(TelegramAdapter):
    """Telegram adapter whose Bot API calls go to a recording mock."""

    def __init__(self, post_json: AsyncMock) -> None:
        super().__init__()
        self._recorder = post_json

    async def _post_json(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._recorder(method, payload)
        return result


class _ContendedLock(asyncio.Lock):
    """Decision lock that signals when a second caller has to wait for it."""

    def __init__(self) -> None:
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self) -> Literal[True]:
        if self.locked():
            self.contended.set()
        return await super().acquire()


def _adapter(post_json: AsyncMock) -> _RecordingAdapter:
    """A freshly started adapter: its callback registry holds no tokens."""
    adapter = _RecordingAdapter(post_json)
    adapter._callback_registry = TelegramCallbackRegistry()
    adapter._client = MagicMock()
    adapter._api_base = "https://api.telegram.org/bottest-token"
    return adapter


def _manager(decision: _Decision, adapter: TelegramAdapter) -> MagicMock:
    manager = MagicMock()
    manager._store = decision.store
    manager.decision_locks = DecisionLocks()
    manager._channel_by_name = {"telegram": decision.channel}
    manager._adapters = {"telegram": adapter}
    manager.admit_inbound_message = AsyncMock(return_value=True)
    manager._identity_manager.resolve_inbound_identity.return_value = IdentityResolution(
        identity=decision.identity, session_id=decision.session_id
    )
    manager.get_voice_transcriber.return_value = None
    manager.get_vision_extract_service.return_value = None
    manager.handle_session_action = AsyncMock(return_value=False)
    manager.event_callback = None
    manager.reaction_handler = None
    return manager


def _click(
    adapter: TelegramAdapter,
    data: str,
    query_id: str,
    *,
    chat_id: str = _CHAT_ID,
    message_id: int = _SOURCE_MESSAGE_ID,
) -> CommsMessage:
    payload: dict[str, Any] = {
        "update_id": 10002,
        "callback_query": {
            "id": query_id,
            "from": {"id": 1111111, "username": "josh"},
            "message": {
                "message_id": message_id,
                "chat": {"id": int(chat_id), "type": "private"},
            },
            "data": data,
        },
    }
    return adapter.parse_webhook(payload, {})[0]


def _calls(post_json: AsyncMock, method: str) -> list[dict[str, Any]]:
    return [c.args[1] for c in post_json.await_args_list if c.args[0] == method]


def _republished(post_json: AsyncMock) -> tuple[str, list[dict[str, str]]]:
    """The text and buttons of the last keyboard republish sent to Telegram."""
    calls = [
        payload for payload in _calls(post_json, "editMessageText") if "reply_markup" in payload
    ]
    assert calls, "expected the decision to be republished"
    payload = calls[-1]
    assert payload["chat_id"] == _CHAT_ID
    assert payload["message_id"] == str(_SOURCE_MESSAGE_ID)
    buttons: list[dict[str, str]] = payload["reply_markup"]["inline_keyboard"][0]
    return payload["text"], buttons


def _reissued_buttons(post_json: AsyncMock) -> list[dict[str, str]]:
    return _republished(post_json)[1]


def _reissued_tokens(post_json: AsyncMock) -> list[str]:
    return [button["callback_data"] for button in _reissued_buttons(post_json)]


def _row(decision: _Decision) -> dict[str, Any]:
    row = decision.store.get_message(decision.source.id)
    assert row is not None
    return row.metadata_json


def _routed(decision: _Decision, query_id: str) -> CommsMessage | None:
    return decision.store.get_message_by_platform_id("telegram", f"callback:{query_id}")


async def _reissue(adapter: _RecordingAdapter, inbound: InboundCommunications) -> list[str]:
    handled = await inbound.handle_messages("telegram", [_click(adapter, "gobby:lost", "q-0")])
    assert handled[0].metadata_json["callback_status"] == "reissued"
    return _reissued_tokens(adapter._recorder)


@pytest.mark.asyncio
async def test_restart_lost_click_reissues_buttons_that_route_once_to_original_session(
    decision: _Decision,
) -> None:
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    stale = _click(adapter, "gobby:token-from-before-restart", "q-stale")
    assert stale.metadata_json["callback_status"] == "invalid"
    handled = await inbound.handle_messages("telegram", [stale])

    assert handled[0].metadata_json["callback_status"] == "reissued"
    text, _ = _republished(post_json)
    assert text == "Approve the design?"
    approve_token, changes_token = _reissued_tokens(post_json)
    assert _row(decision).get("callback_state") is None
    assert _row(decision)["callback_generation"] == 1

    post_json.reset_mock()
    await adapter.acknowledge_webhook_messages(handled)
    answer = _calls(post_json, "answerCallbackQuery")[0]
    assert answer["show_alert"] is True
    assert "Current buttons are attached" in answer["text"]

    routed = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert routed[0].metadata_json["callback_status"] == "ok"
    assert routed[0].session_id == decision.session_id
    assert routed[0].content == "approve"
    assert _row(decision)["callback_state"] == "answered"

    # The sibling button is still a live token, but the decision is closed.
    second = await inbound.handle_messages("telegram", [_click(adapter, changes_token, "q-2")])
    assert second[0].metadata_json["callback_status"] == "answered"
    assert _routed(decision, "q-2") is None

    # A later stale click cannot revive the answered decision.
    post_json.reset_mock()
    late = await inbound.handle_messages("telegram", [_click(adapter, "gobby:1.gone", "q-late")])
    assert late[0].metadata_json["callback_status"] == "answered"
    assert _calls(post_json, "editMessageText") == []


@pytest.mark.asyncio
async def test_failed_answer_persistence_leaves_decision_pending(decision: _Decision) -> None:
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))
    approve_token, _ = await _reissue(adapter, inbound)

    with patch.object(
        decision.store, "_insert_message", side_effect=RuntimeError("database write failed")
    ):
        handled = await inbound.handle_messages(
            "telegram", [_click(adapter, approve_token, "q-ok")]
        )

    assert handled == []  # not acknowledged: polling redelivers the update
    assert _row(decision).get("callback_state") is None
    assert _routed(decision, "q-ok") is None

    # The redelivered click's token was consumed, so it reissues; the fresh
    # buttons then answer the still-pending decision exactly once.
    post_json.reset_mock()
    retry = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert retry[0].metadata_json["callback_status"] == "reissued"
    fresh_approve, _ = _reissued_tokens(post_json)
    routed = await inbound.handle_messages("telegram", [_click(adapter, fresh_approve, "q-ok2")])
    assert routed[0].metadata_json["callback_status"] == "ok"
    assert _row(decision)["callback_state"] == "answered"


@pytest.mark.asyncio
async def test_concurrent_answers_route_exactly_once(decision: _Decision) -> None:
    adapter = _adapter(AsyncMock(return_value=_OK))
    inbound = InboundCommunications(_manager(decision, adapter))
    approve_token, changes_token = await _reissue(adapter, inbound)

    first, second = await asyncio.gather(
        inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-a")]),
        inbound.handle_messages("telegram", [_click(adapter, changes_token, "q-b")]),
    )

    statuses = sorted(
        [first[0].metadata_json["callback_status"], second[0].metadata_json["callback_status"]]
    )
    assert statuses == ["answered", "ok"]
    assert [_routed(decision, "q-a") is None, _routed(decision, "q-b") is None].count(False) == 1


@pytest.mark.asyncio
async def test_live_token_sent_from_another_decision_cannot_answer_it(
    decision: _Decision,
) -> None:
    other = decision.store.create_message(
        CommsMessage(
            id="",
            channel_id=decision.channel.id,
            direction="outbound",
            content="Merge the migration?",
            platform_message_id="100",
            session_id=decision.session_id,
            metadata_json={
                "platform_destination": _CHAT_ID,
                "inline_keyboard": [[{"text": "Merge", "value": "merge"}]],
            },
            created_at=_TS,
        )
    )
    adapter = _adapter(AsyncMock(return_value=_OK))
    inbound = InboundCommunications(_manager(decision, adapter))
    approve_token, _ = await _reissue(adapter, inbound)

    # A client replays the design decision's live token from the migration message.
    crossed = _click(adapter, approve_token, "q-crossed", message_id=100)
    assert crossed.metadata_json["callback_status"] == "invalid"
    await inbound.handle_messages("telegram", [crossed])

    assert _routed(decision, "q-crossed") is None
    migration = decision.store.get_message(other.id)
    assert migration is not None
    assert migration.metadata_json.get("callback_state") is None
    assert _row(decision).get("callback_state") is None
    # The token was not consumed, so the real click still answers its own decision.
    routed = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert routed[0].content == "approve"


@pytest.mark.asyncio
async def test_long_decision_is_republished_in_place_and_answered_once(
    decision: _Decision,
) -> None:
    long_prompt = "Review the plan below.\n" + "word " * 1000  # two Telegram chunks
    long_decision = decision.store.create_message(
        CommsMessage(
            id="",
            channel_id=decision.channel.id,
            direction="outbound",
            content=long_prompt,
            platform_message_id="200",
            session_id=decision.session_id,
            metadata_json={
                "platform_destination": _CHAT_ID,
                "platform_message_ids": ["200", "201"],
                "inline_keyboard": [[{"text": "Approve", "value": "approve"}]],
            },
            created_at=_TS,
        )
    )
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    # After a restart, the expired button sits on the second chunk.
    stale = await inbound.handle_messages(
        "telegram", [_click(adapter, "gobby:gone", "q-stale", message_id=201)]
    )

    assert stale[0].metadata_json["callback_status"] == "reissued"
    edits = _calls(post_json, "editMessageText")
    assert [edit["message_id"] for edit in edits] == ["200", "201"]
    assert edits[0]["text"].startswith("Review the plan below.")
    assert "reply_markup" not in edits[0]
    assert _calls(post_json, "sendMessage") == []
    token = edits[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]

    routed = await inbound.handle_messages(
        "telegram", [_click(adapter, token, "q-ok", message_id=201)]
    )
    assert routed[0].metadata_json["callback_status"] == "ok"
    assert routed[0].session_id == decision.session_id
    stored = decision.store.get_message(long_decision.id)
    assert stored is not None
    assert stored.metadata_json["callback_state"] == "answered"
    assert stored.metadata_json["platform_message_ids"] == ["200", "201"]
    again = await inbound.handle_messages(
        "telegram", [_click(adapter, "gobby:1.gone", "q-again", message_id=201)]
    )
    assert again[0].metadata_json["callback_status"] == "answered"


@pytest.mark.asyncio
async def test_stale_click_waiting_on_an_edit_cannot_revive_the_old_keyboard(
    decision: _Decision,
) -> None:
    edit_publishing = asyncio.Event()
    release_edit = asyncio.Event()

    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "editMessageText":
            edit_publishing.set()
            await release_edit.wait()
        return _OK

    post_json = AsyncMock(side_effect=telegram_api)
    adapter = _adapter(post_json)
    manager = _manager(decision, adapter)
    lock = _ContendedLock()
    manager.decision_locks = lambda _decision_id: lock
    inbound = InboundCommunications(manager)
    replacement = [[{"text": "Ship", "value": "ship"}]]

    edit = asyncio.create_task(
        edit_keyboard_message(
            manager,
            adapter,
            decision.source.id,
            "Ship it?",
            _CHAT_ID,
            replacement,
        )
    )
    await edit_publishing.wait()
    stale = asyncio.create_task(
        inbound.handle_messages("telegram", [_click(adapter, "gobby:old", "q-stale")])
    )
    await lock.contended.wait()
    release_edit.set()
    await edit
    handled = await stale

    # The stale click waited for the edit, then republished the edited keyboard.
    assert handled[0].metadata_json["callback_status"] == "reissued"
    buttons = _reissued_buttons(post_json)
    assert [button["text"] for button in buttons] == ["Ship"]
    assert buttons[0]["callback_data"].startswith("gobby:2.")
    assert _row(decision)["inline_keyboard"] == replacement
    assert _row(decision)["callback_generation"] == 2


@pytest.mark.asyncio
async def test_answer_to_a_replaced_keyboard_republishes_the_current_one(
    decision: _Decision,
) -> None:
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    manager = _manager(decision, adapter)
    inbound = InboundCommunications(manager)
    approve_token, _ = await _reissue(adapter, inbound)
    resolved = _click(adapter, approve_token, "q-ok")
    assert resolved.metadata_json["callback_status"] == "ok"

    await edit_keyboard_message(
        manager,
        adapter,
        decision.source.id,
        "Ship it?",
        _CHAT_ID,
        [[{"text": "Ship", "value": "ship"}]],
    )
    handled = await inbound.handle_messages("telegram", [resolved])

    assert handled[0].metadata_json["callback_status"] == "reissued"
    assert [button["text"] for button in _reissued_buttons(post_json)] == ["Ship"]
    assert _routed(decision, "q-ok") is None
    assert _row(decision).get("callback_state") is None
    assert _row(decision)["callback_generation"] == 3


@pytest.mark.asyncio
async def test_failed_reissue_reports_retry_and_the_next_click_repairs_it(
    decision: _Decision,
) -> None:
    failing = AsyncMock(return_value={"ok": False, "description": "Bad Request"})
    adapter = _adapter(failing)
    inbound = InboundCommunications(_manager(decision, adapter))

    handled = await inbound.handle_messages("telegram", [_click(adapter, "gobby:lost", "q-1")])

    # The generation advanced before the publish failed; Telegram still shows gen 0.
    assert handled[0].metadata_json["callback_status"] == "retry"
    assert _row(decision)["callback_generation"] == 1
    assert adapter._callback_registry._entries == {}
    failing.reset_mock()
    await adapter.acknowledge_webhook_messages(handled)
    assert "could not be refreshed" in _calls(failing, "answerCallbackQuery")[0]["text"]

    failing.return_value = _OK
    retried = await inbound.handle_messages("telegram", [_click(adapter, "gobby:lost", "q-2")])
    assert retried[0].metadata_json["callback_status"] == "reissued"
    approve_token, _ = _reissued_tokens(failing)
    assert approve_token.startswith("gobby:2.")
    routed = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert routed[0].metadata_json["callback_status"] == "ok"
    assert _row(decision)["callback_state"] == "answered"


@pytest.mark.asyncio
async def test_refused_keyboard_edit_restores_the_live_decision(decision: _Decision) -> None:
    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "editMessageText" and payload["text"] != "Approve the design?":
            return {"ok": False, "description": "Bad Request: message can't be edited"}
        return _OK

    post_json = AsyncMock(side_effect=telegram_api)
    adapter = _adapter(post_json)
    manager = _manager(decision, adapter)
    inbound = InboundCommunications(manager)
    approve_token, _ = await _reissue(adapter, inbound)

    with pytest.raises(RuntimeError, match="editMessageText failed"):
        await edit_keyboard_message(
            manager,
            adapter,
            decision.source.id,
            "Ship it?",
            _CHAT_ID,
            [[{"text": "Ship", "value": "ship"}]],
        )

    restored = decision.store.get_message(decision.source.id)
    assert restored is not None
    assert restored.content == "Approve the design?"
    assert restored.metadata_json["inline_keyboard"] == _KEYBOARD
    assert restored.metadata_json["callback_generation"] == 1
    routed = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert routed[0].metadata_json["callback_status"] == "ok"
    assert routed[0].content == "approve"


@pytest.mark.asyncio
async def test_ambiguous_keyboard_edit_failure_keeps_the_staged_keyboard(
    decision: _Decision,
) -> None:
    dropped: list[dict[str, Any]] = []

    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "editMessageText" and payload["text"] == "Ship it?" and not dropped:
            dropped.append(payload)
            raise httpx.ConnectError("connection reset")
        return _OK

    post_json = AsyncMock(side_effect=telegram_api)
    adapter = _adapter(post_json)
    manager = _manager(decision, adapter)
    inbound = InboundCommunications(manager)
    approve_token, _ = await _reissue(adapter, inbound)

    with pytest.raises(httpx.ConnectError):
        await edit_keyboard_message(
            manager,
            adapter,
            decision.source.id,
            "Ship it?",
            _CHAT_ID,
            [[{"text": "Ship", "value": "ship"}]],
        )

    # Telegram may have applied the edit, so the row stays at the staged keyboard.
    assert _row(decision)["callback_generation"] == 2
    post_json.reset_mock()
    handled = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-old")])
    assert handled[0].metadata_json["callback_status"] == "reissued"
    assert _routed(decision, "q-old") is None
    # The new choices go out with the new question, whichever text Telegram held.
    text, buttons = _republished(post_json)
    assert text == "Ship it?"
    assert [button["text"] for button in buttons] == ["Ship"]
    ship_token = buttons[0]["callback_data"]
    routed = await inbound.handle_messages("telegram", [_click(adapter, ship_token, "q-ok")])
    assert routed[0].content == "ship"
    assert _row(decision)["callback_state"] == "answered"


@pytest.mark.asyncio
async def test_refused_button_removal_keeps_the_decision_pending(decision: _Decision) -> None:
    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "editMessageText" and payload["text"] != "Approve the design?":
            return {"ok": False, "description": "Bad Request: message can't be edited"}
        return _OK

    adapter = _adapter(AsyncMock(side_effect=telegram_api))
    manager = _manager(decision, adapter)
    inbound = InboundCommunications(manager)
    approve_token, _ = await _reissue(adapter, inbound)

    with pytest.raises(RuntimeError, match="editMessageText failed"):
        await edit_keyboard_message(
            manager,
            adapter,
            decision.source.id,
            "Withdrawn",
            _CHAT_ID,
            None,
        )

    assert _row(decision).get("callback_state") is None
    routed = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert routed[0].metadata_json["callback_status"] == "ok"


@pytest.mark.asyncio
async def test_superseded_decision_is_refused_without_reissue(decision: _Decision) -> None:
    assert decision.store.stage_callback_edit(decision.source.id, 0, "Withdrawn", None)
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    handled = await inbound.handle_messages("telegram", [_click(adapter, "gobby:old", "q-1")])

    assert handled[0].metadata_json["callback_status"] == "superseded"
    assert _calls(post_json, "editMessageText") == []


@pytest.mark.asyncio
async def test_click_from_another_chat_is_not_reissued(decision: _Decision) -> None:
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    click = _click(adapter, "gobby:old", "q-1", chat_id="3333333")
    handled = await inbound.handle_messages("telegram", [click])

    assert handled[0].metadata_json["callback_status"] == "invalid"
    assert _calls(post_json, "editMessageText") == []
    assert _row(decision).get("callback_state") is None


@pytest.mark.asyncio
async def test_action_keyboard_is_not_reissued(decision: _Decision) -> None:
    decision.store.update_message_delivery(
        decision.source.id,
        "sent",
        None,
        str(_SOURCE_MESSAGE_ID),
        {**decision.source.metadata_json, "callback_action": "agent_menu"},
    )
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    handled = await inbound.handle_messages("telegram", [_click(adapter, "gobby:old", "q-1")])

    assert handled[0].metadata_json["callback_status"] == "invalid"
    assert _calls(post_json, "editMessageText") == []


def test_decision_state_transitions_are_compare_and_set(decision: _Decision) -> None:
    store = decision.store
    source_id = decision.source.id
    replacement = [[{"text": "Ship", "value": "ship"}]]

    # Two writers that both observed generation 0: one wins.
    assert store.claim_callback_reissue(source_id, 0) is True
    assert store.claim_callback_reissue(source_id, 0) is False
    assert store.stage_callback_edit(source_id, 0, "Ship it?", replacement) is False
    assert store.stage_callback_edit(source_id, 1, "Ship it?", replacement) is True
    assert _row(decision)["inline_keyboard"] == replacement

    def answer(query_id: str, generation: int) -> CommsMessage | None:
        callback = CommsMessage(
            id="",
            channel_id=decision.channel.id,
            direction="inbound",
            content="ship",
            content_type="callback",
            platform_message_id=f"callback:{query_id}",
            created_at=_TS,
        )
        return store.accept_callback_decision(source_id, generation, callback)

    assert answer("q-old", 1) is None
    assert answer("q-new", 2) is not None
    assert answer("q-again", 2) is None
    # Removing the buttons of an answered decision keeps its answer.
    assert store.stage_callback_edit(source_id, 2, "Shipped", None) is True
    assert _row(decision)["callback_state"] == "answered"
    assert _routed(decision, "q-old") is None
    assert _routed(decision, "q-again") is None
