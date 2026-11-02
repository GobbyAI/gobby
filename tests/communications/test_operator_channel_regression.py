"""Operator Telegram regression guard (#25590): inbound updates through the real ingest path.

Each case drives TelegramAdapter.poll against a fake Bot API transport, then the
real CommunicationsManager and LocalCommunicationsStore, so the assertions read
comms_messages rows and WARNING log records rather than mock calls.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.config.communications import ChannelDefaults, CommunicationsConfig
from gobby.sessions.processor_stats import ProcessorStatsMixin
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

_FIXED_TS = datetime(2024, 1, 1, tzinfo=UTC)
_OPERATOR_CHAT = "42"
_GROUP_CHAT = "-100777"


def _config() -> CommunicationsConfig:
    return CommunicationsConfig(
        enabled=True,
        channel_defaults=ChannelDefaults(rate_limit_per_minute=60, burst=10),
    )


def _channel() -> ChannelConfig:
    return ChannelConfig(
        id=str(uuid.uuid4()),
        channel_type="telegram",
        name="gobby-telegram",
        enabled=True,
        config_json={"allow_from": [_OPERATOR_CHAT]},
        created_at=_FIXED_TS,
        updated_at=_FIXED_TS,
    )


def _manager(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    channel: ChannelConfig,
) -> tuple[CommunicationsManager, LocalCommunicationsStore]:
    store = LocalCommunicationsStore(temp_db, project_id=sample_project["id"])
    store.create_channel(channel)
    secret_store = MagicMock()
    secret_store.get.return_value = None
    manager = CommunicationsManager(_config(), store, secret_store, SessionManager(temp_db))
    manager._channel_by_name[channel.name] = channel
    return manager, store


def _adapter(
    responses: list[dict[str, Any]],
    *,
    offset: int,
    persist: AsyncMock,
    requests: list[httpx.Request],
) -> TelegramAdapter:
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = queue.pop(0) if queue else {"ok": True, "result": []}
        return httpx.Response(200, json=body, request=request)

    adapter = TelegramAdapter()
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    adapter._api_base = "https://api.telegram.org/botTEST"
    adapter._offset = offset
    adapter._persisted_offset = offset
    adapter.set_config_update_callback(persist)
    return adapter


async def _ingest_once(
    manager: CommunicationsManager,
    channel: ChannelConfig,
    adapter: TelegramAdapter,
) -> list[CommsMessage]:
    """Mirror PollingManager._poll_loop: poll, handle, then acknowledge what was handled."""
    manager._adapters[channel.name] = adapter
    messages = await adapter.poll()
    if not messages:
        return []
    handled = await manager.handle_inbound_messages(channel.name, messages)
    if handled:
        await adapter.acknowledge_messages(handled)
    return handled


def _dm(update_id: int, message_id: int, *, user: int = 42, **fields: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": message_id,
        "from": {"id": user, "is_bot": False, "username": "josh"},
        "chat": {"id": _OPERATOR_CHAT, "type": "private"},
        "date": 1_700_000_000,
        **fields,
    }
    return {"update_id": update_id, "message": message}


def _inbound(store: LocalCommunicationsStore, channel: ChannelConfig) -> list[CommsMessage]:
    return store.list_messages(channel_id=channel.id, direction="inbound", limit=200)


@pytest.mark.asyncio
async def test_long_message_with_entities_is_stored(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    channel = _channel()
    manager, store = _manager(temp_db, sample_project, channel)
    # A message over 4096 characters arrives as two consecutive updates, N and N+1.
    first_text = "Plan " * 800
    second_text = "Plan " * 200
    assert len(first_text + second_text) > 4096
    first_entities = [
        {"type": "bold", "offset": 0, "length": 4},
        {"type": "code", "offset": 5, "length": 4},
    ]
    second_entities = [{"type": "italic", "offset": 0, "length": 4}]
    adapter = _adapter(
        [
            {
                "ok": True,
                "result": [
                    _dm(700, 9100, text=first_text, entities=first_entities),
                    _dm(701, 9101, text=second_text, entities=second_entities),
                ],
            }
        ],
        offset=0,
        persist=AsyncMock(),
        requests=[],
    )

    await _ingest_once(manager, channel, adapter)

    rows = _in_send_order(_inbound(store, channel))
    assert [row.content for row in rows] == [first_text, second_text]
    assert [row.platform_message_id for row in rows] == ["9100", "9101"]


@pytest.mark.asyncio
async def test_forwarded_message_is_stored(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    channel = _channel()
    manager, store = _manager(temp_db, sample_project, channel)
    forwarded = {
        "type": "user",
        "date": 1_700_000_000,
        "sender_user": {"id": 7, "is_bot": False, "first_name": "Forwarder"},
    }
    adapter = _adapter(
        [
            {
                "ok": True,
                "result": [_dm(701, 9101, text="forwarded text", forward_origin=forwarded)],
            }
        ],
        offset=0,
        persist=AsyncMock(),
        requests=[],
    )

    await _ingest_once(manager, channel, adapter)

    assert [row.content for row in _inbound(store, channel)] == ["forwarded text"]


@pytest.mark.asyncio
async def test_update_matching_earlier_outbound_platform_id_is_stored(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    channel = _channel()
    manager, store = _manager(temp_db, sample_project, channel)
    store.create_message(
        CommsMessage(
            id=str(uuid.uuid4()),
            channel_id=channel.id,
            direction="outbound",
            content="earlier group reply",
            platform_message_id="9102",
            metadata_json={"platform_destination": _GROUP_CHAT},
            created_at=_FIXED_TS,
        )
    )
    adapter = _adapter(
        [{"ok": True, "result": [_dm(702, 9102, text="after outbound")]}],
        offset=0,
        persist=AsyncMock(),
        requests=[],
    )

    await _ingest_once(manager, channel, adapter)

    assert [row.content for row in _inbound(store, channel)] == ["after outbound"]


@pytest.mark.asyncio
async def test_unsupported_message_is_recorded_and_logged_at_warning(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = _channel()
    manager, store = _manager(temp_db, sample_project, channel)
    adapter = _adapter(
        [{"ok": True, "result": [_dm(703, 9103, location={"latitude": 1.0, "longitude": 2.0})]}],
        offset=0,
        persist=AsyncMock(),
        requests=[],
    )

    with caplog.at_level(logging.WARNING, logger="gobby.communications"):
        await _ingest_once(manager, channel, adapter)

    rows = _inbound(store, channel)
    assert [row.content_type for row in rows] == ["unsupported"]
    assert rows[0].platform_message_id == "9103"
    assert any(
        record.levelno == logging.WARNING and "703" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rejected_sender_is_logged_at_warning(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = _channel()
    manager, store = _manager(temp_db, sample_project, channel)
    adapter = _adapter(
        [{"ok": True, "result": [_dm(704, 9104, user=99, text="from a stranger")]}],
        offset=0,
        persist=AsyncMock(),
        requests=[],
    )

    with caplog.at_level(logging.WARNING, logger="gobby.communications"):
        await _ingest_once(manager, channel, adapter)

    assert _inbound(store, channel) == []
    assert any(
        record.levelno == logging.WARNING and "9104" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_non_message_update_is_logged_at_warning_and_acknowledged(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = _channel()
    manager, _store = _manager(temp_db, sample_project, channel)
    persist = AsyncMock()
    adapter = _adapter(
        [{"ok": True, "result": [{"update_id": 705, "my_chat_member": {"chat": {"id": 1}}}]}],
        offset=0,
        persist=persist,
        requests=[],
    )

    with caplog.at_level(logging.WARNING, logger="gobby.communications"):
        await _ingest_once(manager, channel, adapter)

    assert adapter._offset == 706
    persist.assert_awaited_with({"poll_offset": 706})
    assert any(
        record.levelno == logging.WARNING and "705" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_poll_offset_survives_restart_without_loss_or_duplicates(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    channel = _channel()
    manager, store = _manager(temp_db, sample_project, channel)
    first_persist = AsyncMock()
    first = _adapter(
        [
            {
                "ok": True,
                "result": [
                    _dm(600, 9200, text="first"),
                    _dm(601, 9201, location={"latitude": 1.0, "longitude": 2.0}),
                ],
            }
        ],
        offset=0,
        persist=first_persist,
        requests=[],
    )
    await _ingest_once(manager, channel, first)
    first_persist.assert_awaited_with({"poll_offset": 602})

    # Daemon restart: a new adapter restores the persisted offset from channel config.
    restart_persist = AsyncMock()
    requests: list[httpx.Request] = []
    restarted = _adapter(
        [{"ok": True, "result": [_dm(602, 9202, text="third")]}],
        offset=602,
        persist=restart_persist,
        requests=requests,
    )
    await _ingest_once(manager, channel, restarted)

    assert requests[0].url.params["offset"] == "602"
    restart_persist.assert_awaited_with({"poll_offset": 603})
    rows = _inbound(store, channel)
    assert sorted(row.platform_message_id or "" for row in rows) == ["9200", "9201", "9202"]


class _Host(ProcessorStatsMixin):
    """Real broadcast and mirror methods, without a websocket or processor state."""

    def __init__(self, manager: CommunicationsManager) -> None:
        self.communications_manager = manager
        self.websocket_server = None
        self._mirror_eligible_ids: dict[str, set[str]] = {}
        self._mirror_start_offsets: dict[str, int] = {}


def _sender(payloads: list[str]) -> TelegramAdapter:
    """Telegram adapter whose sendMessage calls are recorded by the fake transport."""

    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content)["text"])
        body = {"ok": True, "result": {"message_id": 5000 + len(payloads)}}
        return httpx.Response(200, json=body, request=request)

    adapter = TelegramAdapter()
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    adapter._api_base = "https://api.telegram.org/botTEST"
    return adapter


def _attached_turn(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    manager: CommunicationsManager,
    channel: ChannelConfig,
) -> str:
    channel.config_json["operator_project_id"] = sample_project["id"]
    session = SessionManager(temp_db).register(
        "operator-turn", None, "claude", sample_project["id"]
    )
    manager.attach_conversation(channel.name, f"dm:{_OPERATOR_CHAT}", session.id)
    return session.id


def _outbound(store: LocalCommunicationsStore, channel: ChannelConfig) -> list[CommsMessage]:
    return store.list_messages(channel_id=channel.id, direction="outbound", limit=200)


def _last_line(text: str) -> str:
    return text.rsplit("\n", 1)[-1]


def _in_send_order(rows: list[CommsMessage]) -> list[CommsMessage]:
    """Store listings are newest first; Telegram ids increase with arrival or send order."""
    return sorted(rows, key=lambda row: int(row.platform_message_id or 0))


@pytest.mark.asyncio
async def test_multi_text_block_turn_is_mirrored_in_order(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    channel = _channel()
    manager, store = _manager(temp_db, sample_project, channel)
    session_id = _attached_turn(temp_db, sample_project, manager, channel)
    payloads: list[str] = []
    manager._adapters[channel.name] = _sender(payloads)
    rendered = {
        "id": "turn-multi",
        "role": "assistant",
        "content_blocks": [
            {"type": "text", "content": "Before tool."},
            {"type": "tool_use", "content": "ls"},
            {"type": "text", "content": "After tool."},
        ],
    }

    await _Host(manager)._broadcast_rendered_session_message(session_id, rendered, complete=True)

    assert [_last_line(text) for text in payloads] == ["Before tool.", "After tool."]
    assert [_last_line(row.content) for row in _in_send_order(_outbound(store, channel))] == [
        "Before tool.",
        "After tool.",
    ]


@pytest.mark.asyncio
async def test_single_text_turn_is_mirrored_once(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    channel = _channel()
    manager, store = _manager(temp_db, sample_project, channel)
    session_id = _attached_turn(temp_db, sample_project, manager, channel)
    payloads: list[str] = []
    manager._adapters[channel.name] = _sender(payloads)
    rendered = {
        "id": "turn-single",
        "role": "assistant",
        "content_blocks": [{"type": "text", "content": "Only final."}],
    }

    await _Host(manager)._broadcast_rendered_session_message(session_id, rendered, complete=True)

    assert [_last_line(text) for text in payloads] == ["Only final."]
    assert [_last_line(row.content) for row in _outbound(store, channel)] == ["Only final."]
