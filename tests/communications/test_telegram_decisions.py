"""Durable Telegram decision-keyboard settlement against the real communications store."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter, TelegramEditNotApplied
from gobby.communications.chat_backend import ChatSessionCommsBackend
from gobby.communications.identities import IdentityManager, IdentityResolution
from gobby.communications.inbound import InboundCommunications
from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.communications.responder import CommunicationsResponder
from gobby.communications.telegram_actions import TelegramActionController
from gobby.communications.telegram_callbacks import TelegramCallbackRegistry
from gobby.communications.telegram_decisions import DecisionLocks, edit_keyboard_message
from gobby.config.communications import CommunicationsConfig
from gobby.sessions.mailbox import MailboxService
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.decision_answers import DecisionAnswerStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.inter_session_messages import InterSessionMessageManager
from gobby.storage.sessions import SessionManager, system_session_id

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

    # The one accepted answer waits in the asking session's mailbox under its own ID.
    mailbox = InterSessionMessageManager(decision.store.db).get_undelivered_messages(
        decision.session_id
    )
    assert [message.id for message in mailbox] == [routed[0].id]
    assert mailbox[0].content.endswith("was answered: approve")
    # The daemon delivers it; the metadata names who clicked, never the session as sender.
    assert mailbox[0].from_session == system_session_id()
    assert mailbox[0].message_type == "telegram_message"
    metadata = json.loads(mailbox[0].metadata_json or "{}")
    # The fixture's asker is a comms session: the responder delivers, so no wake.
    assert routed[0].answer_delivery == "responder"
    assert metadata["wake_requested"] is False
    assert metadata["sender"] == "1111111"
    assert metadata["comms_identity_id"] == decision.identity.id
    assert metadata["comms_decision_id"] == decision.source.id
    assert metadata["comms_answer_id"] == routed[0].id
    assert metadata["callback_data"] == "approve"

    # A later stale click cannot revive the answered decision.
    post_json.reset_mock()
    late = await inbound.handle_messages("telegram", [_click(adapter, "gobby:1.gone", "q-late")])
    assert late[0].metadata_json["callback_status"] == "answered"
    assert _calls(post_json, "editMessageText") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_write",
    [
        (LocalCommunicationsStore, "_insert_message"),
        (InterSessionMessageManager, "create_message"),
    ],
)
async def test_failed_answer_persistence_leaves_decision_pending(
    decision: _Decision, failing_write: tuple[type, str]
) -> None:
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))
    approve_token, _ = await _reissue(adapter, inbound)

    owner, method = failing_write
    with patch.object(owner, method, side_effect=RuntimeError("database write failed")):
        handled = await inbound.handle_messages(
            "telegram", [_click(adapter, approve_token, "q-ok")]
        )

    assert handled == []  # not acknowledged: polling redelivers the update
    assert _row(decision).get("callback_state") is None
    assert _routed(decision, "q-ok") is None
    mailbox = InterSessionMessageManager(decision.store.db)
    assert mailbox.get_undelivered_messages(decision.session_id) == []

    # The redelivered click's token was consumed, so it reissues; the fresh
    # buttons then answer the still-pending decision exactly once.
    post_json.reset_mock()
    retry = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert retry[0].metadata_json["callback_status"] == "reissued"
    fresh_approve, _ = _reissued_tokens(post_json)
    routed = await inbound.handle_messages("telegram", [_click(adapter, fresh_approve, "q-ok2")])
    assert routed[0].metadata_json["callback_status"] == "ok"
    assert _row(decision)["callback_state"] == "answered"
    assert [message.id for message in mailbox.get_undelivered_messages(decision.session_id)] == [
        routed[0].id
    ]


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

    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Republishing the unchanged question leaves the first chunk as it was.
        if method == "editMessageText" and payload["message_id"] == "200":
            request = httpx.Request("POST", "https://api.telegram.org/bot***/editMessageText")
            raise httpx.HTTPStatusError(
                "Client error '400 Bad Request'",
                request=request,
                response=httpx.Response(
                    400,
                    request=request,
                    json={"ok": False, "description": "Bad Request: message is not modified"},
                ),
            )
        return _OK

    post_json = AsyncMock(side_effect=telegram_api)
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

    post_json.reset_mock()
    with pytest.raises(ValueError, match="is answered; send a new decision"):
        await edit_keyboard_message(
            manager,
            adapter,
            decision.source.id,
            "Ship it again?",
            _CHAT_ID,
            [[{"text": "Ship", "value": "ship"}]],
        )
    assert post_json.await_count == 0
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


def _markup_data(payload: dict[str, Any]) -> list[str]:
    markup = payload.get("reply_markup") or {"inline_keyboard": []}
    return [button["callback_data"] for row in markup["inline_keyboard"] for button in row]


def _growing_api(
    decision: _Decision, refuse_attach: bool = False
) -> tuple[AsyncMock, list[object]]:
    recorded_at_attach: list[object] = []

    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": 300}}
        if method == "editMessageReplyMarkup" and payload.get("reply_markup"):
            recorded_at_attach.append(_row(decision).get("platform_message_ids"))
            if refuse_attach:
                return {"ok": False, "description": "Bad Request: message can't be edited"}
        return _OK

    return AsyncMock(side_effect=telegram_api), recorded_at_attach


_LONG_QUESTION = "Ship it?\n" + "word " * 1000  # two Telegram chunks
_SHIP = [[{"text": "Ship", "value": "ship"}]]


@pytest.mark.asyncio
async def test_growing_edit_publishes_new_buttons_last_on_a_recorded_chunk(
    decision: _Decision,
) -> None:
    post_json, recorded_at_attach = _growing_api(decision)
    adapter = _adapter(post_json)
    manager = _manager(decision, adapter)
    inbound = InboundCommunications(manager)
    await _reissue(adapter, inbound)
    post_json.reset_mock()

    await edit_keyboard_message(
        manager, adapter, decision.source.id, _LONG_QUESTION, _CHAT_ID, _SHIP
    )

    steps = [(call.args[0], call.args[1].get("message_id")) for call in post_json.await_args_list]
    root = str(_SOURCE_MESSAGE_ID)
    assert steps == [
        ("sendMessage", None),
        ("editMessageText", root),
        ("editMessageReplyMarkup", "300"),
        ("editMessageReplyMarkup", root),
    ]
    # The old buttons stay clickable, as recovery buttons, until the new ones are out.
    assert _markup_data(post_json.await_args_list[1].args[1]) == ["gobby:recover"] * 2
    assert "reply_markup" not in post_json.await_args_list[3].args[1]
    assert recorded_at_attach == [[root, "300"]]
    ship_token = _markup_data(post_json.await_args_list[2].args[1])[0]

    # After a restart the button on the added chunk still finds its decision.
    restarted = _adapter(post_json)
    manager._adapters["telegram"] = restarted
    lost = await inbound.handle_messages(
        "telegram", [_click(restarted, ship_token, "q-lost", message_id=300)]
    )
    assert lost[0].metadata_json["callback_status"] == "reissued"


@pytest.mark.asyncio
async def test_failed_chunk_record_restores_the_untouched_decision(decision: _Decision) -> None:
    post_json, _ = _growing_api(decision)
    adapter = _adapter(post_json)
    manager = _manager(decision, adapter)
    inbound = InboundCommunications(manager)
    approve_token, _ = await _reissue(adapter, inbound)
    post_json.reset_mock()

    with (
        patch.object(
            decision.store,
            "record_platform_message_ids",
            side_effect=ConnectionError("database unavailable"),
        ),
        pytest.raises(TelegramEditNotApplied, match="database unavailable"),
    ):
        await edit_keyboard_message(
            manager, adapter, decision.source.id, _LONG_QUESTION, _CHAT_ID, _SHIP
        )

    # Only the button-less added chunk went out; the decision and its buttons are intact.
    assert [call.args[0] for call in post_json.await_args_list] == ["sendMessage"]
    assert "reply_markup" not in _calls(post_json, "sendMessage")[0]
    assert _row(decision)["callback_generation"] == 1
    routed = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert routed[0].metadata_json["callback_status"] == "ok"


@pytest.mark.asyncio
async def test_partly_applied_edit_stays_staged_behind_recovery_buttons(
    decision: _Decision,
) -> None:
    post_json, _ = _growing_api(decision, refuse_attach=True)
    adapter = _adapter(post_json)
    manager = _manager(decision, adapter)
    inbound = InboundCommunications(manager)
    approve_token, _ = await _reissue(adapter, inbound)

    with pytest.raises(RuntimeError, match="editMessageReplyMarkup failed"):
        await edit_keyboard_message(
            manager, adapter, decision.source.id, _LONG_QUESTION, _CHAT_ID, _SHIP
        )

    # Telegram shows the new text, so the row is not rolled back to the old question.
    assert _row(decision)["callback_generation"] == 2
    assert _row(decision)["platform_message_ids"] == [str(_SOURCE_MESSAGE_ID), "300"]
    old = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-old")])
    assert old[0].metadata_json["callback_status"] == "reissued"
    assert _routed(decision, "q-old") is None
    recovery = await inbound.handle_messages(
        "telegram", [_click(adapter, "gobby:recover", "q-recover")]
    )
    assert recovery[0].metadata_json["callback_status"] == "reissued"


@pytest.mark.asyncio
async def test_refused_shrinking_edit_keeps_the_buttons_on_the_last_chunk(
    decision: _Decision,
) -> None:
    long_decision = decision.store.create_message(
        CommsMessage(
            id="",
            channel_id=decision.channel.id,
            direction="outbound",
            content="Review the plan below.\n" + "word " * 1000,
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

    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "editMessageText":
            return {"ok": False, "description": "Bad Request: message can't be edited"}
        return _OK

    post_json = AsyncMock(side_effect=telegram_api)
    adapter = _adapter(post_json)
    with pytest.raises(TelegramEditNotApplied):
        await edit_keyboard_message(
            _manager(decision, adapter), adapter, long_decision.id, "Short now?", _CHAT_ID, _SHIP
        )

    assert _calls(post_json, "deleteMessage") == []
    restored = decision.store.get_message(long_decision.id)
    assert restored is not None
    assert restored.content.startswith("Review the plan below.")
    assert restored.metadata_json["platform_message_ids"] == ["200", "201"]


@pytest.mark.asyncio
async def test_undeletable_surplus_chunk_stays_recorded(decision: _Decision) -> None:
    long_decision = decision.store.create_message(
        CommsMessage(
            id="",
            channel_id=decision.channel.id,
            direction="outbound",
            content="Review the plan below.\n" + "word " * 1000,
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

    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "deleteMessage":
            return {"ok": False, "description": "Bad Request: message can't be deleted"}
        return _OK

    adapter = _adapter(AsyncMock(side_effect=telegram_api))
    manager = _manager(decision, adapter)
    await edit_keyboard_message(
        manager,
        adapter,
        long_decision.id,
        "Short now?",
        _CHAT_ID,
        [[{"text": "Yes", "value": "yes"}]],
    )

    stored = decision.store.get_message(long_decision.id)
    assert stored is not None
    assert stored.metadata_json["platform_message_ids"] == ["200", "201"]
    # The old buttons left on chunk 201 still lead back to the decision after a restart.
    restarted = _adapter(AsyncMock(side_effect=telegram_api))
    manager._adapters["telegram"] = restarted
    handled = await InboundCommunications(manager).handle_messages(
        "telegram", [_click(restarted, "gobby:0.gone", "q-old", message_id=201)]
    )
    assert handled[0].metadata_json["callback_status"] == "reissued"


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("click_chat_id", "bound_message_id", "action"),
    [
        pytest.param("3333333", _SOURCE_MESSAGE_ID, None, id="wrong_chat"),
        pytest.param(_CHAT_ID, _SOURCE_MESSAGE_ID, "agent_menu", id="action_keyboard"),
        pytest.param(_CHAT_ID, 555, None, id="unpersisted_source"),
    ],
)
async def test_expired_known_token_outside_a_persisted_decision_resolves_invalid(
    decision: _Decision, click_chat_id: str, bound_message_id: int, action: str | None
) -> None:
    if action is not None:
        decision.store.update_message_delivery(
            decision.source.id,
            "sent",
            None,
            str(_SOURCE_MESSAGE_ID),
            {**decision.source.metadata_json, "callback_action": action},
        )
    now = [1000.0]
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    adapter._callback_registry = TelegramCallbackRegistry(clock=lambda: now[0])
    markup = adapter._callback_registry.register_keyboard(
        _KEYBOARD,
        session_id=decision.session_id,
        chat_id=_CHAT_ID,
        thread_id=None,
        ttl_seconds=30,
        action=action,
        source_id=decision.source.id,
    )
    adapter._callback_registry.bind_keyboard(markup, str(bound_message_id))
    token = markup["inline_keyboard"][0][0]["callback_data"]
    now[0] += 31
    inbound = InboundCommunications(_manager(decision, adapter))

    click = _click(adapter, token, "q-1", chat_id=click_chat_id, message_id=bound_message_id)
    handled = await inbound.handle_messages("telegram", [click])

    assert handled[0].metadata_json["callback_status"] == "invalid"
    assert _calls(post_json, "editMessageText") == []
    assert _calls(post_json, "editMessageReplyMarkup") == []
    assert _row(decision).get("callback_state") is None
    assert _routed(decision, "q-1") is None


@pytest.mark.asyncio
async def test_expired_known_token_without_a_source_message_resolves_invalid(
    decision: _Decision,
) -> None:
    now = [1000.0]
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    adapter._callback_registry = TelegramCallbackRegistry(clock=lambda: now[0])
    markup = adapter._callback_registry.register_keyboard(
        _KEYBOARD,
        session_id=decision.session_id,
        chat_id=_CHAT_ID,
        thread_id=None,
        ttl_seconds=30,
        source_id=decision.source.id,
    )
    token = markup["inline_keyboard"][0][0]["callback_data"]
    now[0] += 31
    inbound = InboundCommunications(_manager(decision, adapter))
    click = _click(adapter, token, "q-1")
    click.metadata_json.pop("callback_source_message_id", None)

    handled = await inbound.handle_messages("telegram", [click])

    assert handled[0].metadata_json["callback_status"] == "invalid"
    assert _calls(post_json, "editMessageText") == []
    assert _routed(decision, "q-1") is None


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
    # New buttons never reopen an answered decision.
    assert store.stage_callback_edit(source_id, 2, "Ship again?", replacement) is False
    assert _row(decision)["callback_state"] == "answered"
    # Removing the buttons of an answered decision keeps its answer.
    assert store.stage_callback_edit(source_id, 2, "Shipped", None) is True
    assert _row(decision)["callback_state"] == "answered"
    assert _routed(decision, "q-old") is None
    assert _routed(decision, "q-again") is None


class _WakeRecorder:
    def __init__(self, error: Exception | None = None) -> None:
        self.woken: list[str] = []
        self._error = error

    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]:
        self.woken.append(session_id)
        if self._error is not None:
            raise self._error
        return {"session_id": session_id, "delivered": True, "method": "terminal"}


class _ChatHost:
    """The websocket chat host's turn surface; fails the first ``failures`` turns.

    With a ``gate``, every turn waits for it after it starts.
    """

    def __init__(self, failures: int = 0, gate: asyncio.Event | None = None) -> None:
        self.turns: list[tuple[str, str]] = []
        self.resets: list[str] = []
        self._failures = failures
        self._gate = gate

    async def configure_chat_session(
        self, conversation_id: str, *, chat_mode: str, agent_name: str, project_id: str
    ) -> None:
        return None

    async def _run_chat_turn(self, **kwargs: Any) -> None:
        self.turns.append((str(kwargs["conversation_id"]), str(kwargs["content"])))
        if self._gate is not None:
            await self._gate.wait()
        if self._failures:
            self._failures -= 1
            raise RuntimeError("chat turn failed")

    async def reset_chat_session(self, conversation_id: str) -> bool:
        self.resets.append(conversation_id)
        return False

    def resolve_chat_binding(
        self, conversation_id: str, *, provider: str | None, model: str | None
    ) -> tuple[str, str | None]:
        return provider or "claude", model


@dataclass
class _AnswerRoute:
    inbound: InboundCommunications
    adapter: _RecordingAdapter
    post_json: AsyncMock
    responder: CommunicationsResponder
    host: _ChatHost
    wakes: _WakeRecorder
    observed: list[CommsMessage]
    approve_token: str
    manager: MagicMock
    answers: DecisionAnswerStore
    backend: ChatSessionCommsBackend


async def _answer_route(
    decision: _Decision,
    asker_source: str,
    *,
    wake_error: Exception | None = None,
    failed_turns: int = 0,
    gate: asyncio.Event | None = None,
) -> _AnswerRoute:
    """Real accept, action, mailbox and responder paths on a responder-enabled channel."""
    post_json = AsyncMock(return_value=_OK)
    adapter = _adapter(post_json)
    manager = _manager(decision, adapter)
    sessions = SessionManager(decision.store.db)
    decision.store.db.execute(
        "UPDATE sessions SET source = %s WHERE id = %s", (asker_source, decision.session_id)
    )
    sessions.update_status(decision.session_id, "paused")
    wakes = _WakeRecorder(wake_error)
    mailbox = MailboxService(
        db=decision.store.db,
        message_manager=InterSessionMessageManager(decision.store.db),
        session_manager=sessions,
        wake_dispatcher=wakes,
    )
    manager.get_channel_by_name.return_value = decision.channel
    manager.get_channel.return_value = replace(
        decision.channel,
        config_json={"responder": {"enabled": True}, "allow_from": ["1111111"]},
    )
    manager.handle_session_action = TelegramActionController(manager, sessions, mailbox).handle
    host = _ChatHost(failed_turns, gate)
    answers = DecisionAnswerStore(decision.store.db, machine_id=decision.store.machine_id)
    manager.decision_answers = answers
    backend = ChatSessionCommsBackend(host, manager)
    responder = CommunicationsResponder(
        manager,
        backend=backend,
        answers=answers,
        daemon_epoch="live-daemon-epoch",
        answer_status=lambda answer: CommunicationsManager._publish_answer_status(manager, answer),
        retry_delays=(0.01,),
    )
    manager.responder = responder
    observed: list[CommsMessage] = []

    async def fan_out(event: str, **kwargs: Any) -> None:
        message = kwargs["message"]
        assert isinstance(message, CommsMessage)
        observed.append(message)
        await responder.handle_event(event, **kwargs)

    manager.event_callback = fan_out
    inbound = InboundCommunications(manager)
    approve_token, _ = await _reissue(adapter, inbound)
    await responder.drain()
    observed.clear()
    host.turns.clear()
    post_json.reset_mock()
    return _AnswerRoute(
        inbound,
        adapter,
        post_json,
        responder,
        host,
        wakes,
        observed,
        approve_token,
        manager,
        answers,
        backend,
    )


async def _click_approve(route: _AnswerRoute) -> CommsMessage:
    routed = await route.inbound.handle_messages(
        "telegram", [_click(route.adapter, route.approve_token, "q-ok")]
    )
    await route.responder.drain()
    assert routed[0].metadata_json["callback_status"] == "ok"
    return routed[0]


def _undelivered(decision: _Decision) -> list[str]:
    mailbox = InterSessionMessageManager(decision.store.db)
    return [message.id for message in mailbox.get_undelivered_messages(decision.session_id)]


def _outcome(decision: _Decision, answer_id: str) -> str | None:
    answer = decision.store.get_message(answer_id)
    assert answer is not None
    return answer.answer_outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_error", [None, ConnectionError("tmux unavailable")])
async def test_cli_asker_reads_its_answer_once_from_the_mailbox(
    decision: _Decision, wake_error: Exception | None
) -> None:
    route = await _answer_route(decision, "claude", wake_error=wake_error)

    answer = await _click_approve(route)

    assert answer.answer_delivery == "mailbox"
    assert route.wakes.woken == [decision.session_id]
    assert _undelivered(decision) == [answer.id]
    # Observers still see the click; the responder never delivers it a second time.
    assert [message.id for message in route.observed] == [answer.id]
    assert route.host.turns == []
    # A failed wake leaves the row for wake recovery and never asks for a resend.
    assert _calls(route.post_json, "sendMessage") == []


@pytest.mark.asyncio
async def test_comms_asker_gets_its_answer_as_one_turn_in_its_own_chat(
    decision: _Decision,
) -> None:
    route = await _answer_route(decision, "comms")

    answer = await _click_approve(route)

    assert answer.answer_delivery == "responder"
    # A comms session has no wake route or mailbox reader; its chat turn is the delivery.
    assert route.wakes.woken == []
    assert route.host.turns == [(decision.session_id, "approve")]
    assert _undelivered(decision) == []
    assert _outcome(decision, answer.id) == "delivered"
    delivery = InterSessionMessageManager(decision.store.db).get_message(answer.id)
    assert delivery is not None
    assert delivery.from_session == system_session_id()
    assert json.loads(delivery.metadata_json or "{}")["wake_requested"] is False

    # A second queueing of the same answer (recovery racing the live event) is a no-op.
    await route.responder.handle_message(answer)
    await route.responder.recover_decision_answers()
    await route.responder.drain()
    assert len(route.host.turns) == 1


@pytest.mark.asyncio
async def test_failed_comms_answer_turn_is_recorded_and_never_replayed(
    decision: _Decision,
) -> None:
    route = await _answer_route(decision, "comms", failed_turns=1)

    answer = await _click_approve(route)

    # The turn may have acted before failing, so replaying it could repeat those actions.
    assert route.host.turns == [(decision.session_id, "approve")]
    assert _outcome(decision, answer.id) == "failed"
    await route.responder.recover_decision_answers()
    await route.responder.drain()
    assert route.host.turns == [(decision.session_id, "approve")]
    assert _undelivered(decision) == []


@pytest.mark.asyncio
async def test_command_shaped_decision_value_is_answered_not_run(decision: _Decision) -> None:
    decision.store.db.execute(
        """UPDATE comms_messages
              SET metadata_json = jsonb_set(metadata_json, '{inline_keyboard}', %s::jsonb)
            WHERE id = %s""",
        (
            json.dumps([[{"text": "Start over", "value": "/reset"}, _KEYBOARD[0][1]]]),
            decision.source.id,
        ),
    )
    route = await _answer_route(decision, "comms")

    answer = await _click_approve(route)

    assert route.host.turns == [(decision.session_id, "/reset")]
    assert route.host.resets == []
    assert _outcome(decision, answer.id) == "delivered"


@pytest.mark.asyncio
async def test_sender_revoked_before_the_click_leaves_the_decision_open(
    decision: _Decision,
) -> None:
    route = await _answer_route(decision, "comms")
    route.manager.admit_inbound_message.return_value = False

    await route.inbound.handle_messages(
        "telegram", [_click(route.adapter, route.approve_token, "q-denied")]
    )
    await route.responder.drain()

    assert "callback_state" not in _row(decision)
    assert route.host.turns == []
    # Once readmitted, the spent button reissues the still-open decision.
    route.manager.admit_inbound_message.return_value = True
    spent = await route.inbound.handle_messages(
        "telegram", [_click(route.adapter, route.approve_token, "q-spent")]
    )
    assert spent[0].metadata_json["callback_status"] == "reissued"
    route.approve_token = _reissued_tokens(route.adapter._recorder)[0]
    await _click_approve(route)
    assert route.host.turns == [(decision.session_id, "approve")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revoked",
    [
        {"responder": {"enabled": False}, "allow_from": ["1111111"]},
        {"responder": {"enabled": True}, "allow_from": ["9999999"]},
    ],
    ids=["responder-disabled", "sender-removed"],
)
async def test_policy_revoked_after_the_click_blocks_the_answer_durably(
    decision: _Decision, revoked: dict[str, object]
) -> None:
    route = await _answer_route(decision, "comms")
    route.responder.set_backend(None)
    answer = await _click_approve(route)
    assert _undelivered(decision) == [answer.id]

    route.manager.get_channel.return_value = replace(decision.channel, config_json=revoked)
    route.responder.set_backend(route.backend)
    await route.responder.recover_decision_answers()
    await route.responder.drain()
    await route.responder.recover_decision_answers()
    await route.responder.drain()

    assert route.host.turns == []
    assert _outcome(decision, answer.id) == "blocked"
    assert _undelivered(decision) == []
    # The decision says why the answer went nowhere, once, and offers no retry.
    [status] = _status_edits(route)
    assert "not delivered" in status["text"]
    assert _markup_data(status) == []


def _status_edits(route: _AnswerRoute) -> list[dict[str, Any]]:
    """Decision republishes that carry an answer status line."""
    return [
        payload
        for payload in _calls(route.post_json, "editMessageText")
        if "Answer “" in payload["text"] or "Retrying answer" in payload["text"]
    ]


def _retry_token(route: _AnswerRoute) -> str:
    """The single Retry answer button on the latest status."""
    status = _status_edits(route)[-1]
    [button] = status["reply_markup"]["inline_keyboard"][0]
    assert button["text"] == "Retry answer"
    token: str = button["callback_data"]
    return token


async def _press(route: _AnswerRoute, token: str, query_id: str) -> CommsMessage:
    handled = await route.inbound.handle_messages(
        "telegram", [_click(route.adapter, token, query_id)]
    )
    await route.responder.drain()
    return handled[0]


async def _until(check: Callable[[], object]) -> None:
    async with asyncio.timeout(5):
        while not check():
            await asyncio.sleep(0.01)


def _answer(decision: _Decision, answer_id: str) -> CommsMessage:
    answer = decision.store.get_message(answer_id)
    assert answer is not None
    return answer


@pytest.mark.asyncio
async def test_failed_answer_offers_one_retry_that_runs_once_more(decision: _Decision) -> None:
    route = await _answer_route(decision, "comms", failed_turns=1)
    answer = await _click_approve(route)

    assert _outcome(decision, answer.id) == "failed"
    failed = _status_edits(route)[-1]
    # A failed turn may have acted, so the status never claims it had no effect.
    assert "may have partly acted" in failed["text"]
    assert "may repeat actions" in failed["text"]
    retry = _retry_token(route)

    clicked = await _press(route, retry, "q-retry")

    assert clicked.metadata_json["callback_status"] == "retrying"
    assert len(route.host.turns) == 2
    retried_turn = route.host.turns[1][1]
    assert retried_turn.startswith("[Retry of a Telegram decision answer, attempt 2.")
    assert retried_turn.endswith("\napprove")
    retried = _answer(decision, answer.id)
    assert (retried.answer_outcome, retried.answer_attempt) == ("delivered", 2)
    assert retried.content == "approve"
    assert "delivered on retry (attempt 2)" in _status_edits(route)[-1]["text"]

    # The button is spent: a second click on it never starts another attempt.
    again = await _press(route, retry, "q-retry-again")
    assert again.metadata_json["callback_status"] == "answered"
    assert len(route.host.turns) == 2


@pytest.mark.asyncio
async def test_repeated_retry_taps_start_one_attempt(decision: _Decision) -> None:
    route = await _answer_route(decision, "comms", failed_turns=1)
    answer = await _click_approve(route)
    retry = _retry_token(route)

    handled = await route.inbound.handle_messages(
        "telegram", [_click(route.adapter, retry, f"q-retry-{n}") for n in range(3)]
    )
    await route.responder.drain()

    assert [click.metadata_json["callback_status"] for click in handled] == [
        "retrying",
        "answered",
        "answered",
    ]
    assert len(route.host.turns) == 2
    assert _answer(decision, answer.id).answer_attempt == 2


@pytest.mark.asyncio
async def test_retry_click_from_a_revoked_sender_consumes_nothing(decision: _Decision) -> None:
    route = await _answer_route(decision, "comms", failed_turns=1)
    answer = await _click_approve(route)
    retry = _retry_token(route)
    route.manager.admit_inbound_message.return_value = False

    await _press(route, retry, "q-denied-retry")

    failed = _answer(decision, answer.id)
    assert (failed.answer_outcome, failed.answer_attempt) == ("failed", 1)
    assert len(route.host.turns) == 1


@pytest.mark.asyncio
async def test_retry_on_a_lapsed_button_reissues_it(decision: _Decision) -> None:
    route = await _answer_route(decision, "comms", failed_turns=1)
    answer = await _click_approve(route)
    # A restarted daemon has no live callback tokens.
    route.adapter._callback_registry = TelegramCallbackRegistry()

    lapsed = await _press(route, "gobby:lost", "q-lapsed")

    assert lapsed.metadata_json["callback_status"] == "reissued"
    await _press(route, _retry_token(route), "q-reissued-retry")
    assert len(route.host.turns) == 2
    assert _outcome(decision, answer.id) == "delivered"


@pytest.mark.asyncio
async def test_turn_claimed_by_an_earlier_daemon_is_in_doubt_not_rerun(
    decision: _Decision,
) -> None:
    route = await _answer_route(decision, "comms")
    route.responder.set_backend(None)
    answer = await _click_approve(route)
    assert route.answers.claim_answer(answer.id, 1, "dead-daemon-epoch")

    route.responder.set_backend(route.backend)
    await route.responder.recover_decision_answers()
    await route.responder.drain()

    assert route.host.turns == []
    assert _outcome(decision, answer.id) == "in_doubt"
    assert "interrupted" in _status_edits(route)[-1]["text"]
    await _press(route, _retry_token(route), "q-retry")
    assert [content.endswith("\napprove") for _, content in route.host.turns] == [True]
    assert _outcome(decision, answer.id) == "delivered"


@pytest.mark.asyncio
async def test_recovery_leaves_this_daemons_running_turn_alone(decision: _Decision) -> None:
    gate = asyncio.Event()
    route = await _answer_route(decision, "comms", gate=gate)
    await route.inbound.handle_messages(
        "telegram", [_click(route.adapter, route.approve_token, "q-ok")]
    )
    await _until(lambda: route.host.turns)
    answer_id = route.observed[0].id

    await route.responder.recover_decision_answers()

    assert _outcome(decision, answer_id) == "started"
    gate.set()
    await route.responder.drain()
    assert _outcome(decision, answer_id) == "delivered"
    assert len(route.host.turns) == 1


@pytest.mark.asyncio
async def test_cancelled_answer_turn_is_shown_in_doubt(decision: _Decision) -> None:
    route = await _answer_route(decision, "comms", gate=asyncio.Event())
    await route.inbound.handle_messages(
        "telegram", [_click(route.adapter, route.approve_token, "q-ok")]
    )
    await _until(lambda: route.host.turns)
    answer_id = route.observed[0].id

    await route.responder.stop()

    assert _outcome(decision, answer_id) == "in_doubt"
    assert "interrupted" in _status_edits(route)[-1]["text"]
    assert _retry_token(route)


@pytest.mark.asyncio
async def test_answer_behind_a_full_conversation_is_delivered_without_restart(
    decision: _Decision,
) -> None:
    gate = asyncio.Event()
    route = await _answer_route(decision, "comms", gate=gate)
    route.responder.set_backend(None)
    answer = await _click_approve(route)
    route.responder.set_backend(route.backend)
    chatter = {
        key: value
        for key, value in answer.metadata_json.items()
        if not key.startswith(("answer_", "callback_"))
    }
    for n in range(8):
        await route.responder.handle_message(
            replace(answer, id=f"chatter-{n}", content=f"chatter {n}", metadata_json=chatter)
        )

    assert await route.responder.handle_message(answer) is None

    assert _outcome(decision, answer.id) == "pending"
    gate.set()
    await _until(lambda: _outcome(decision, answer.id) == "delivered")
    assert route.host.turns[-1] == (decision.session_id, "approve")
    assert len(route.host.turns) == 9


@pytest.mark.asyncio
async def test_backend_installed_after_recovery_routes_waiting_answers(
    decision: _Decision,
) -> None:
    route = await _answer_route(decision, "comms")
    route.responder.set_backend(None)
    answer = await _click_approve(route)
    await route.responder.recover_decision_answers()
    assert _outcome(decision, answer.id) == "pending"

    route.responder.set_backend(route.backend)

    await _until(lambda: _outcome(decision, answer.id) == "delivered")
    assert route.host.turns == [(decision.session_id, "approve")]


@pytest.mark.asyncio
async def test_status_lost_to_telegram_is_retried_until_shown(decision: _Decision) -> None:
    route = await _answer_route(decision, "comms", failed_turns=1)
    outage = {"failed_edits": 0}

    async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "editMessageText" and outage["failed_edits"] < 2:
            outage["failed_edits"] += 1
            raise httpx.ConnectError("telegram unreachable")
        return _OK

    route.post_json.side_effect = telegram_api
    answer = await _click_approve(route)

    # No restart or recovery call: the status reaches Telegram once it is back.
    await _until(
        lambda: _answer(decision, answer.id).metadata_json.get("answer_status_shown") == "1:failed"
    )
    assert outage["failed_edits"] == 2
    assert _retry_token(route)
    assert len(route.host.turns) == 1
    shown = len(_status_edits(route))
    await route.responder.recover_decision_answers()
    assert len(_status_edits(route)) == shown


@pytest.mark.asyncio
async def test_status_waits_for_an_absent_telegram_adapter(decision: _Decision) -> None:
    gate = asyncio.Event()
    route = await _answer_route(decision, "comms", failed_turns=1, gate=gate)
    await route.inbound.handle_messages(
        "telegram", [_click(route.adapter, route.approve_token, "q-ok")]
    )
    await _until(lambda: route.host.turns)
    answer = route.observed[0]
    # The Telegram channel goes away while the answer's turn is running.
    route.manager._adapters = {}
    gate.set()
    await route.responder.drain()
    assert _outcome(decision, answer.id) == "failed"
    assert _status_edits(route) == []

    route.manager._adapters = {"telegram": route.adapter}

    await _until(
        lambda: _answer(decision, answer.id).metadata_json.get("answer_status_shown") == "1:failed"
    )
    assert _retry_token(route)
    assert len(route.host.turns) == 1


@pytest.mark.asyncio
async def test_answer_transitions_are_bound_to_their_attempt(decision: _Decision) -> None:
    route = await _answer_route(decision, "comms", failed_turns=1)
    answer = await _click_approve(route)
    answers = route.answers

    assert answers.consume_retry(answer.id, 1, "1111111") is not None
    assert answers.consume_retry(answer.id, 1, "1111111") is None
    assert answers.claim_answer(answer.id, 2, "live-daemon-epoch")
    # A late writer for attempt 1 cannot settle attempt 2.
    assert answers.settle_answer(answer.id, 1, "failed") is None
    assert _outcome(decision, answer.id) == "started"
    # Another machine's daemon never sweeps this machine's answers.
    other_machine = DecisionAnswerStore(
        decision.store.db, machine_id="0b9f3c2e-5d6a-4e1f-9a7b-3c8d2e1f0a9b"
    )
    assert other_machine.sweep_in_doubt_answers("another-epoch") == []
    assert [swept.id for swept in answers.sweep_in_doubt_answers("next-epoch")] == [answer.id]


@pytest.mark.asyncio
async def test_retry_reapplies_current_policy_and_blocks_without_a_button(
    decision: _Decision,
) -> None:
    route = await _answer_route(decision, "comms", failed_turns=1)
    answer = await _click_approve(route)
    retry = _retry_token(route)
    # The sender may still click, but the responder no longer serves this chat.
    route.manager.get_channel.return_value = replace(
        decision.channel, config_json={"responder": {"enabled": False}}
    )

    await _press(route, retry, "q-retry")

    blocked = _answer(decision, answer.id)
    assert (blocked.answer_outcome, blocked.answer_attempt) == ("blocked", 2)
    assert len(route.host.turns) == 1
    status = _status_edits(route)[-1]
    assert "not delivered" in status["text"]
    assert _markup_data(status) == []


@pytest.mark.asyncio
async def test_transient_ledger_failures_back_off_until_the_answer_is_delivered(
    decision: _Decision, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = await _answer_route(decision, "comms")
    route.responder.set_backend(None)
    answer = await _click_approve(route)
    calls = {"sweep": 0, "claim": 0}
    sweep = route.answers.sweep_in_doubt_answers
    claim = route.answers.claim_answer

    def flaky_sweep(epoch: str) -> list[CommsMessage]:
        calls["sweep"] += 1
        if calls["sweep"] == 1:
            raise ConnectionError("database restarting")
        return sweep(epoch)

    def flaky_claim(answer_id: str, attempt: int, epoch: str) -> bool:
        calls["claim"] += 1
        if calls["claim"] == 1:
            raise ConnectionError("database restarting")
        return claim(answer_id, attempt, epoch)

    monkeypatch.setattr(route.answers, "sweep_in_doubt_answers", flaky_sweep)
    monkeypatch.setattr(route.answers, "claim_answer", flaky_claim)
    route.responder.set_backend(route.backend)

    await route.responder.recover_decision_answers()

    await _until(lambda: _outcome(decision, answer.id) == "delivered")
    assert route.host.turns == [(decision.session_id, "approve")]
    assert calls == {"sweep": 2, "claim": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retry_shaped", ["gobby:retry-answer:1", "1"], ids=["former-retry-prefix", "attempt-number"]
)
async def test_retry_shaped_option_on_a_pending_decision_is_an_ordinary_answer(
    decision: _Decision, retry_shaped: str
) -> None:
    # A Retry answer button's value is its attempt number; only its trusted callback
    # action, never an option value, makes a click a retry.
    keyboard = [[{"text": "Pick", "value": retry_shaped}, _KEYBOARD[0][1]]]
    decision.store.db.execute(
        """UPDATE comms_messages
              SET metadata_json = jsonb_set(metadata_json, '{inline_keyboard}', %s::jsonb)
            WHERE id = %s""",
        (json.dumps(keyboard), decision.source.id),
    )
    route = await _answer_route(decision, "comms")

    answer = await _click_approve(route)

    assert answer.content == retry_shaped
    assert _row(decision)["callback_state"] == "answered"
    assert route.host.turns == [(decision.session_id, retry_shaped)]
    assert _outcome(decision, answer.id) == "delivered"
