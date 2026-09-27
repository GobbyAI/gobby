"""Durable Telegram decision-keyboard settlement against the real communications store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.identities import IdentityManager, IdentityResolution
from gobby.communications.inbound import InboundCommunications
from gobby.communications.models import ChannelConfig, CommsIdentity, CommsMessage
from gobby.communications.telegram_callbacks import TelegramCallbackRegistry
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


def _adapter(post_json: AsyncMock) -> TelegramAdapter:
    """A freshly started adapter: its callback registry holds no tokens."""
    adapter = _RecordingAdapter(post_json)
    adapter._callback_registry = TelegramCallbackRegistry()
    adapter._client = MagicMock()
    adapter._api_base = "https://api.telegram.org/bottest-token"
    return adapter


def _manager(decision: _Decision, adapter: TelegramAdapter) -> MagicMock:
    manager = MagicMock()
    manager._store = decision.store
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
    adapter: TelegramAdapter, data: str, query_id: str, *, chat_id: str = _CHAT_ID
) -> CommsMessage:
    payload: dict[str, Any] = {
        "update_id": 10002,
        "callback_query": {
            "id": query_id,
            "from": {"id": 1111111, "username": "josh"},
            "message": {
                "message_id": _SOURCE_MESSAGE_ID,
                "chat": {"id": int(chat_id), "type": "private"},
            },
            "data": data,
        },
    }
    return adapter.parse_webhook(payload, {})[0]


def _reissued_tokens(post_json: AsyncMock) -> list[str]:
    calls = [c for c in post_json.await_args_list if c.args[0] == "editMessageReplyMarkup"]
    assert calls, "expected the decision buttons to be reissued"
    payload = calls[-1].args[1]
    assert payload["chat_id"] == _CHAT_ID
    assert payload["message_id"] == str(_SOURCE_MESSAGE_ID)
    return [button["callback_data"] for button in payload["reply_markup"]["inline_keyboard"][0]]


def _edit_count(post_json: AsyncMock) -> int:
    return sum(1 for c in post_json.await_args_list if c.args[0] == "editMessageReplyMarkup")


def _state(decision: _Decision) -> object:
    row = decision.store.get_message(decision.source.id)
    assert row is not None
    return row.metadata_json.get("callback_state")


@pytest.mark.asyncio
async def test_restart_lost_click_reissues_buttons_that_route_once_to_original_session(
    decision: _Decision,
) -> None:
    post_json = AsyncMock(return_value={"ok": True, "result": True})
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    stale = _click(adapter, "gobby:token-from-before-restart", "q-stale")
    assert stale.metadata_json["callback_status"] == "invalid"
    handled = await inbound.handle_messages("telegram", [stale])

    assert handled[0].metadata_json["callback_status"] == "reissued"
    approve_token, changes_token = _reissued_tokens(post_json)
    assert _state(decision) is None

    post_json.reset_mock()
    await adapter.acknowledge_webhook_messages(handled)
    answer = post_json.await_args
    assert answer is not None
    assert answer.args[1]["show_alert"] is True
    assert "Fresh buttons" in answer.args[1]["text"]

    routed = await inbound.handle_messages("telegram", [_click(adapter, approve_token, "q-ok")])
    assert routed[0].metadata_json["callback_status"] == "ok"
    assert routed[0].session_id == decision.session_id
    assert routed[0].content == "approve"
    assert _state(decision) == "answered"

    # The sibling button from the same keyboard is still a live token, but the
    # decision is closed: it must not reach the session a second time.
    second = await inbound.handle_messages("telegram", [_click(adapter, changes_token, "q-2")])
    assert second[0].metadata_json["callback_status"] == "answered"
    assert decision.store.get_message_by_platform_id("telegram", "callback:q-2") is None

    # A later stale click cannot revive the answered decision.
    post_json.reset_mock()
    late = await inbound.handle_messages("telegram", [_click(adapter, "gobby:gone", "q-late")])
    assert late[0].metadata_json["callback_status"] == "answered"
    assert _edit_count(post_json) == 0


@pytest.mark.asyncio
async def test_superseded_decision_is_refused_without_reissue(decision: _Decision) -> None:
    decision.store.supersede_callback_decision(decision.source.id)
    post_json = AsyncMock(return_value={"ok": True, "result": True})
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    handled = await inbound.handle_messages("telegram", [_click(adapter, "gobby:old", "q-1")])

    assert handled[0].metadata_json["callback_status"] == "superseded"
    assert _edit_count(post_json) == 0


@pytest.mark.asyncio
async def test_click_from_another_chat_is_not_reissued(decision: _Decision) -> None:
    post_json = AsyncMock(return_value={"ok": True, "result": True})
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    click = _click(adapter, "gobby:old", "q-1", chat_id="3333333")
    handled = await inbound.handle_messages("telegram", [click])

    assert handled[0].metadata_json["callback_status"] == "invalid"
    assert _edit_count(post_json) == 0
    assert _state(decision) is None


@pytest.mark.asyncio
async def test_action_keyboard_is_not_reissued(decision: _Decision) -> None:
    decision.store.update_message_delivery(
        decision.source.id,
        "sent",
        None,
        str(_SOURCE_MESSAGE_ID),
        {**decision.source.metadata_json, "callback_action": "agent_menu"},
    )
    post_json = AsyncMock(return_value={"ok": True, "result": True})
    adapter = _adapter(post_json)
    inbound = InboundCommunications(_manager(decision, adapter))

    handled = await inbound.handle_messages("telegram", [_click(adapter, "gobby:old", "q-1")])

    assert handled[0].metadata_json["callback_status"] == "invalid"
    assert _edit_count(post_json) == 0


def test_concurrent_reissue_claims_admit_one_winner(decision: _Decision) -> None:
    store = decision.store
    source_id = decision.source.id

    # Two stale clicks that both observed generation 0.
    assert store.claim_callback_reissue(source_id, 0) is True
    assert store.claim_callback_reissue(source_id, 0) is False

    assert store.answer_callback_decision(source_id) is True
    assert store.answer_callback_decision(source_id) is False
    assert store.claim_callback_reissue(source_id, 1) is False
    store.supersede_callback_decision(source_id)
    assert _state(decision) == "answered"


def test_replaced_keyboard_is_recorded_for_reissue(decision: _Decision) -> None:
    replacement = [[{"text": "Ship", "value": "ship"}]]

    decision.store.replace_callback_keyboard(decision.source.id, replacement)

    row = decision.store.get_message(decision.source.id)
    assert row is not None
    assert row.metadata_json["inline_keyboard"] == replacement
    assert row.metadata_json["callback_generation"] == 1
    assert row.metadata_json.get("callback_state") is None
