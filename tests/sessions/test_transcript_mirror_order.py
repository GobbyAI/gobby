"""A turn's mirrored text blocks reach Telegram strictly in order, even across a failed send."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.config.communications import CommunicationsConfig
from gobby.sessions.processor_stats import ProcessorStatsMixin
from gobby.sessions.processor_types import ProcessorHost
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.integration
TS = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    now = datetime.now(UTC)

    def read_clock() -> datetime:
        return now

    def advance(seconds: float) -> None:
        nonlocal now
        now += timedelta(seconds=seconds)

    for module in (
        "storage.communications",
        "communications.outbound",
        "communications.manager",
        "communications.transcript_order",
    ):
        monkeypatch.setattr(f"gobby.{module}.utc_now", read_clock)
    return advance


@pytest.fixture
def attached(
    temp_db: HubDatabase, sample_project: dict[str, str]
) -> tuple[CommunicationsManager, str, AsyncMock]:
    sessions = SessionManager(temp_db)
    session = sessions.register("mirror-order", None, "claude", sample_project["id"])
    store = LocalCommunicationsStore(temp_db)
    channel = store.create_channel(
        ChannelConfig(
            id="",
            name="mirror-order",
            channel_type="telegram",
            enabled=True,
            config_json={"operator_project_id": sample_project["id"]},
            created_at=TS,
            updated_at=TS,
        )
    )
    manager = CommunicationsManager(CommunicationsConfig(), store, MagicMock(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = MagicMock()
    adapter.send_message = AsyncMock()
    manager._adapters[channel.name] = adapter
    manager.attach_conversation(channel.name, "dm:42", session.id)
    return manager, session.id, adapter.send_message


async def mirror(manager: CommunicationsManager, session_id: str, texts: list[str]) -> None:
    host = cast(ProcessorHost, SimpleNamespace(communications_manager=manager))
    await ProcessorStatsMixin._mirror_text_blocks(host, session_id, "turn", texts)


def sent(send: AsyncMock) -> list[str]:
    return [call.args[0].content for call in send.call_args_list]


def statuses(manager: CommunicationsManager, session_id: str) -> dict[str, str]:
    return {
        row.metadata_json["transcript_message_id"]: row.status
        for row in manager.store.list_messages(session_id=session_id)
    }


async def test_failed_block_holds_its_suffix_until_the_retry_delivers_it_first(
    attached: tuple[CommunicationsManager, str, AsyncMock], clock: Callable[[float], None]
) -> None:
    manager, session_id, send = attached
    send.side_effect = [RuntimeError("offline"), "p-1", "p-2"]

    with pytest.raises(RuntimeError, match="offline"):
        await mirror(manager, session_id, ["Working.", "Final."])

    assert sent(send) == ["Working."]
    assert statuses(manager, session_id) == {"turn#0": "failed", "turn": "failed"}
    await manager.retry_transcript_deliveries(session_id)
    assert sent(send) == ["Working."]  # The prefix is still backing off; its suffix waits.

    clock(10)
    await manager.retry_transcript_deliveries(session_id)

    assert sent(send) == ["Working.", "Working.", "Final."]
    assert statuses(manager, session_id) == {"turn#0": "sent", "turn": "sent"}


async def test_a_prefix_failing_again_on_retry_keeps_its_suffix_held(
    attached: tuple[CommunicationsManager, str, AsyncMock], clock: Callable[[float], None]
) -> None:
    manager, session_id, send = attached
    send.side_effect = [RuntimeError("offline"), RuntimeError("still offline"), "p-1", "p-2"]
    with pytest.raises(RuntimeError, match="offline"):
        await mirror(manager, session_id, ["Working.", "Final."])

    clock(10)
    await manager.retry_transcript_deliveries(session_id)
    assert sent(send) == ["Working.", "Working."]
    assert statuses(manager, session_id)["turn"] == "failed"

    clock(20)
    await manager.retry_transcript_deliveries(session_id)
    assert sent(send)[2:] == ["Working.", "Final."]


async def test_a_middle_failure_keeps_the_sent_prefix_and_holds_the_rest(
    attached: tuple[CommunicationsManager, str, AsyncMock], clock: Callable[[float], None]
) -> None:
    manager, session_id, send = attached
    send.side_effect = ["p-0", RuntimeError("offline"), "p-1", "p-2"]
    with pytest.raises(RuntimeError, match="offline"):
        await mirror(manager, session_id, ["One.", "Two.", "Three."])

    assert sent(send) == ["One.", "Two."]
    assert statuses(manager, session_id) == {"turn#0": "sent", "turn#1": "failed", "turn": "failed"}

    clock(10)
    await manager.retry_transcript_deliveries(session_id)
    assert sent(send) == ["One.", "Two.", "Two.", "Three."]


async def test_an_exhausted_prefix_releases_its_suffix(
    attached: tuple[CommunicationsManager, str, AsyncMock], clock: Callable[[float], None]
) -> None:
    manager, session_id, send = attached
    send.side_effect = [RuntimeError("down"), RuntimeError("down"), RuntimeError("down"), "p-2"]
    with pytest.raises(RuntimeError, match="down"):
        await mirror(manager, session_id, ["Working.", "Final."])

    clock(10)
    await manager.retry_transcript_deliveries(session_id)
    clock(20)
    await manager.retry_transcript_deliveries(session_id)

    # The third attempt exhausts the prefix, so the same pass delivers the suffix.
    assert sent(send) == ["Working.", "Working.", "Working.", "Final."]
    assert statuses(manager, session_id) == {"turn#0": "failed", "turn": "sent"}


async def test_a_block_whose_reserve_failed_is_held_ahead_of_its_suffix(
    attached: tuple[CommunicationsManager, str, AsyncMock],
    clock: Callable[[float], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session_id, send = attached
    send.side_effect = ["p-1", "p-2"]
    reserve = manager.store.reserve_transcript_message
    calls = 0

    def reserve_failing_once(message: CommsMessage) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transaction failed")
        return reserve(message)

    monkeypatch.setattr(manager.store, "reserve_transcript_message", reserve_failing_once)
    with pytest.raises(RuntimeError, match="transaction failed"):
        await mirror(manager, session_id, ["Working.", "Final."])

    assert sent(send) == []
    assert statuses(manager, session_id) == {"turn#0": "failed", "turn": "failed"}
    await manager.retry_transcript_deliveries(session_id)

    assert sent(send) == ["Working.", "Final."]
    assert statuses(manager, session_id) == {"turn#0": "sent", "turn": "sent"}


async def test_single_and_healthy_turns_send_each_block_once_in_order(
    attached: tuple[CommunicationsManager, str, AsyncMock], clock: Callable[[float], None]
) -> None:
    manager, session_id, send = attached
    send.side_effect = ["p-0", "p-1", "p-2"]

    await mirror(manager, session_id, ["A.", "B."])
    host = cast(ProcessorHost, SimpleNamespace(communications_manager=manager))
    await ProcessorStatsMixin._mirror_text_blocks(host, session_id, "solo", ["Only."])
    await manager.retry_transcript_deliveries(session_id)

    assert sent(send) == ["A.", "B.", "Only."]
    assert statuses(manager, session_id) == {"turn#0": "sent", "turn": "sent", "solo": "sent"}
