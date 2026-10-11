"""Transcript delivery uses durable communications reservations, without a websocket."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.manager import CommunicationsManager
from gobby.communications.models import ChannelConfig
from gobby.config.communications import CommunicationsConfig
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcript_window import WindowResult
from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.integration
TS = datetime(2026, 10, 10, tzinfo=UTC)


@pytest.fixture
def retry_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], datetime]:
    now = datetime.now(UTC)

    def read_clock() -> datetime:
        return now

    def advance(seconds: float) -> datetime:
        nonlocal now
        now += timedelta(seconds=seconds)
        return now

    monkeypatch.setattr("gobby.storage.communications.utc_now", read_clock)
    monkeypatch.setattr("gobby.communications.outbound.utc_now", read_clock)
    return advance


async def test_outage_backoff_survives_polling_and_replay(
    attached: tuple[CommunicationsManager, str, AsyncMock],
    retry_clock: Callable[[float], datetime],
) -> None:
    manager, session_id, send = attached
    send.side_effect = [RuntimeError("offline"), RuntimeError("offline"), "delivered"]
    with pytest.raises(RuntimeError, match="offline"):
        await manager.mirror_session_message(session_id, "outage", "reply")
    retry_clock(8)
    await manager.retry_transcript_deliveries(session_id)
    with pytest.raises(RuntimeError, match="offline"):
        await manager.mirror_session_message(session_id, "outage", "reply")
    send.assert_awaited_once()
    retry_clock(2)
    await manager.retry_transcript_deliveries(session_id)
    assert send.await_count == 2
    retry_clock(19)
    await manager.retry_transcript_deliveries(session_id)
    assert send.await_count == 2
    retry_clock(1)
    await manager.retry_transcript_deliveries(session_id)
    assert send.await_count == 3
    rows = manager.store.list_messages(session_id=session_id)
    assert len(rows) == 1 and rows[0].status == "sent"


@pytest.mark.parametrize("retry_after,attempts", [(60, 4), (1000, 1)])
async def test_telegram_flood_control_delays_transport_and_durable_retry(
    attached: tuple[CommunicationsManager, str, AsyncMock],
    retry_clock: Callable[[float], datetime],
    retry_after: int,
    attempts: int,
) -> None:
    manager, session_id, _ = attached
    adapter = TelegramAdapter()
    requests: list[httpx.Request] = []
    now = retry_clock(0).timestamp()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) <= attempts:
            return httpx.Response(429, json={"parameters": {"retry_after": retry_after}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter._client = client
        adapter._api_base = "https://telegram.test"
        manager._adapters["mirror-test"] = adapter
        with patch(
            "gobby.communications.adapters.base.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            with pytest.raises(RuntimeError, match="429"):
                await manager.mirror_session_message(session_id, "flood", "reply")
        delays = [call.args[0] for call in sleep.await_args_list]
        assert delays == [retry_after] * (attempts - 1)
        assert all(delay <= 120 for delay in delays)
        pending = manager.store.list_messages(session_id=session_id)[0]
        assert pending.metadata_json["transcript_retry_at"] >= now + retry_after
        retry_clock(retry_after - 1)
        await manager.retry_transcript_deliveries(session_id)
        assert len(requests) == attempts
        retry_clock(1)
        await manager.retry_transcript_deliveries(session_id)
    rows = manager.store.list_messages(session_id=session_id)
    assert len(requests) == attempts + 1 and rows[0].status == "sent"


async def test_detach_cancels_retries_before_same_dm_reattachment(
    attached: tuple[CommunicationsManager, str, AsyncMock],
    retry_clock: Callable[[float], datetime],
) -> None:
    manager, session_id, send = attached
    send.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        await manager.mirror_session_message(session_id, "old", "old reply")
    previous = manager.store.list_messages(session_id=session_id)[0]
    manager.detach_conversation("mirror-test", "dm:42", session_id)
    # A late result from the old in-flight attempt cannot revive its retry.
    manager.store.update_message_delivery(
        previous.id, "failed", "late failure", None, previous.metadata_json
    )
    manager.attach_conversation("mirror-test", "dm:42", session_id)
    retry_clock(30)
    await manager.retry_transcript_deliveries(session_id)
    send.assert_awaited_once()
    rows = manager.store.list_messages(session_id=session_id)
    assert rows[0].status == "cancelled"
    assert manager.store.list_retryable_transcript_messages(session_id) == []


@pytest.mark.parametrize("changed", ["destination", "thread"])
async def test_stale_rows_cannot_starve_newer_retry_window(
    attached: tuple[CommunicationsManager, str, AsyncMock],
    retry_clock: Callable[[float], datetime],
    changed: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, session_id, send = attached
    monkeypatch.setattr(manager._rate_limiter, "wait_if_needed", AsyncMock())
    send.side_effect = RuntimeError("offline")
    for index in range(11):
        with pytest.raises(RuntimeError, match="offline"):
            await manager.mirror_session_message(session_id, str(index), str(index))
        row = manager.store.list_messages(session_id=session_id, limit=20)[0]
        if changed == "destination":
            row.metadata_json["platform_destination"] = "old-dm"
        else:
            row.platform_thread_id = "old-thread"
        # Persist the deliberate old destination through the message boundary.
        manager.store.db.execute(
            "UPDATE comms_messages SET platform_thread_id = %s WHERE id = %s",
            (row.platform_thread_id, row.id),
        )
        manager.store.update_message_delivery(row.id, "failed", row.error, None, row.metadata_json)
    with pytest.raises(RuntimeError, match="offline"):
        await manager.mirror_session_message(session_id, "new", "new reply")
    send.side_effect = None
    send.return_value = "delivered"
    retry_clock(30)
    await manager.retry_transcript_deliveries(session_id)
    rows = manager.store.list_messages(session_id=session_id, limit=20)
    assert send.await_count == 13
    assert sum(row.status == "cancelled" for row in rows) == 11
    assert next(row for row in rows if row.content == "new reply").status == "sent"


async def test_unattached_session_never_queries_retry_ledger(
    attached: tuple[CommunicationsManager, str, AsyncMock],
) -> None:
    manager, session_id, _ = attached
    manager.detach_conversation("mirror-test", "dm:42", session_id)
    channel = manager.store.get_channel_by_name("mirror-test")
    assert channel is not None
    assert manager.attached_destination(channel.id, session_id) is None
    with patch.object(manager.store, "list_retryable_transcript_messages") as query:
        await manager.retry_transcript_deliveries(session_id)
    query.assert_not_called()


def record(index: int, role: str, content: str, content_type: str = "text") -> ParsedMessage:
    return ParsedMessage(
        index=index,
        role=role,
        content=content,
        content_type=content_type,
        tool_name=None,
        tool_input=None,
        tool_result=None,
        timestamp=TS,
        raw_json={},
        message_id=f"message-{index}",
        source="claude",
    )


@pytest.fixture
def attached(temp_db: HubDatabase) -> tuple[CommunicationsManager, str, AsyncMock]:
    sessions = SessionManager(temp_db)
    session = sessions.register("mirror-test", None, "claude", None)
    store = LocalCommunicationsStore(temp_db)
    channel = store.create_channel(
        ChannelConfig(
            id="",
            name="mirror-test",
            channel_type="telegram",
            enabled=True,
            config_json={},
            created_at=TS,
            updated_at=TS,
        )
    )
    manager = CommunicationsManager(CommunicationsConfig(), store, MagicMock(), sessions)
    manager._channel_by_name[channel.name] = channel
    adapter = MagicMock()
    adapter.send_message = AsyncMock(side_effect=["platform-1", "platform-2", "platform-3"])
    manager._adapters[channel.name] = adapter
    manager.attach_conversation(channel.name, "dm:42", session.id)
    return manager, session.id, adapter.send_message


def processor(temp_db: HubDatabase, manager: CommunicationsManager) -> SessionMessageProcessor:
    return SessionMessageProcessor(temp_db, communications_manager=manager)


def native_line(role: str, message_id: str, text: str) -> str:
    return (
        json.dumps(
            {
                "type": role,
                "message": {"id": message_id, "content": [{"type": "text", "text": text}]},
            }
        )
        + "\n"
    )


@pytest.mark.parametrize(
    "destination,chat,thread",
    [("dm:42", "42", None), ("topic:-100:7", "-100", 7), ("group:-100", "-100", None)],
)
async def test_mirror_uses_explicit_send_destination_resolution_at_http_boundary(
    attached: tuple[CommunicationsManager, str, AsyncMock],
    destination: str,
    chat: str,
    thread: int | None,
) -> None:
    manager, session_id, _ = attached
    manager.detach_conversation("mirror-test", "dm:42", session_id)
    manager.attach_conversation("mirror-test", destination, session_id)
    payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(payloads)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = TelegramAdapter()
        adapter._client = client
        adapter._api_base = "https://telegram.test"
        manager._adapters["mirror-test"] = adapter
        await manager.send_message("mirror-test", "explicit", session_id=session_id)
        await manager.mirror_session_message(session_id, "reply", "mirrored")
    assert [(payload["chat_id"], payload.get("message_thread_id")) for payload in payloads] == [
        (chat, thread),
        (chat, thread),
    ]
    rows = manager.store.list_messages(session_id=session_id)
    assert len(rows) == 2
    assert all(row.status == "sent" for row in rows)


@pytest.mark.parametrize("failure", ["exhausted", "pending", "mismatch"])
async def test_delivery_failure_keeps_cursor_stats_and_websocket_progressing(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    tmp_path: Path,
    failure: str,
    retry_clock: Callable[[float], datetime],
) -> None:
    manager, session_id, send = attached
    transcript = tmp_path / "ingest.jsonl"
    transcript.touch()
    websocket = MagicMock()
    websocket.broadcast = AsyncMock()
    target = SessionMessageProcessor(
        temp_db,
        communications_manager=manager,
        session_manager=SessionManager(temp_db),
        websocket_server=websocket,
    )
    target.register_session(session_id, str(transcript))
    send.side_effect = RuntimeError("transport down")
    content = native_line("assistant", "a1", "one") + native_line("user", "u1", "next")
    transcript.write_text(content)
    await target._process_all_sessions()
    if failure == "pending":
        row = manager.store.list_messages(session_id=session_id)[0]
        row.metadata_json["transcript_lease_until"] = datetime.now(UTC).timestamp() + 900
        manager.store.update_message_delivery(row.id, "pending", None, None, row.metadata_json)
    for _ in range(4):
        retry_clock(30)
        await target._process_all_sessions()
    assert target._byte_offsets[session_id] == len(content.encode())
    assert target._stats[session_id]["message_count"] == 2
    first_attempts = 1 if failure == "pending" else 3
    assert send.await_count == first_attempts
    second_id = "a1" if failure == "mismatch" else "a2"
    transcript.write_text(
        content + native_line("assistant", second_id, "two") + native_line("user", "u2", "third")
    )
    for _ in range(5):
        retry_clock(30)
        await target._process_all_sessions()
    assert target._byte_offsets[session_id] == transcript.stat().st_size
    assert target._stats[session_id]["message_count"] == 4
    session = SessionManager(temp_db).get(session_id)
    assert session is not None and session.message_count == 4
    events = [call.args[0] for call in websocket.broadcast.call_args_list]
    assert any(event["message"]["content"] == "two" and event["complete"] for event in events)
    rows = manager.store.list_messages(session_id=session_id)
    if failure == "mismatch":
        assert len(rows) == 1 and send.await_count == 3
    else:
        assert len(rows) == 2 and send.await_count == first_attempts + 3
    assert all(
        row.status == "pending" or row.metadata_json["transcript_delivery_attempt"] == 3
        for row in rows
    )
    assert manager.store.list_retryable_transcript_messages(session_id) == []


@pytest.mark.parametrize("mode", ["cold", "degraded-sidecar", "truncated"])
async def test_catchup_skips_history_and_mirrors_only_new_live_reply(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    tmp_path: Path,
    mode: str,
) -> None:
    manager, session_id, send = attached
    transcript = tmp_path / "history.jsonl"
    history = "".join(
        native_line("user", f"u{i}", "old prompt")
        + native_line("assistant", f"a{i}", f"old reply {i}")
        for i in range(3)
    ) + native_line("user", "tail", "old tail")
    transcript.write_text(history)
    original = processor(temp_db, manager)
    original.register_session(session_id, str(transcript))
    if mode != "cold":
        assert (await original.flush_session(session_id)).flushed
    if mode == "degraded-sidecar":
        with patch(
            "gobby.sessions.processor_lifecycle.render_window",
            return_value=WindowResult(
                groups=[], returned_count=0, total_groups=7, parsed_message_count=7, degraded=True
            ),
        ):
            target = processor(temp_db, manager)
            target.register_session(session_id, str(transcript))
    else:
        target = original
    if mode == "truncated":
        history = (
            native_line("user", "old-u", "old")
            + native_line("assistant", "old-a", "old reply")
            + native_line("user", "tail", "old tail")
        )
        transcript.write_text(history)
    assert (await target.flush_session(session_id)).flushed
    send.assert_not_awaited()
    transcript.write_text(
        history
        + native_line("assistant", "live", "new live reply")
        + native_line("user", "live-u", "new prompt")
    )
    assert (await target.flush_session(session_id)).flushed
    send.assert_awaited_once()
    rows = manager.store.list_messages(session_id=session_id)
    assert len(rows) == 1 and rows[0].content == "new live reply"


async def test_expired_last_attempt_is_terminal_without_resending(
    attached: tuple[CommunicationsManager, str, AsyncMock],
) -> None:
    manager, session_id, send = attached
    send.side_effect = RuntimeError("down")
    with pytest.raises(RuntimeError, match="down"):
        await manager.mirror_session_message(session_id, "reply", "whole reply")
    row = manager.store.list_messages(session_id=session_id)[0]
    row.metadata_json.update(transcript_delivery_attempt=3, transcript_lease_until=0)
    manager.store.update_message_delivery(row.id, "pending", None, None, row.metadata_json)
    await manager.retry_transcript_deliveries(session_id)
    send.assert_awaited_once()
    terminal = manager.store.get_message(row.id)
    assert terminal is not None and terminal.status == "failed"
    assert terminal.error == "Transcript delivery attempts exhausted after uncertain request"
    assert manager.store.list_retryable_transcript_messages(session_id) == []


async def test_attachment_after_polling_does_not_admit_an_earlier_pending_reply(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    tmp_path: Path,
) -> None:
    manager, session_id, send = attached
    manager.detach_conversation("mirror-test", "dm:42", session_id)
    transcript = tmp_path / "attach-later.jsonl"
    transcript.touch()
    manager._session_store.update(session_id, transcript_path=str(transcript))
    target = processor(temp_db, manager)
    target.register_session(session_id, str(transcript))
    old = native_line("assistant", "old", "before attachment")
    transcript.write_text(old)
    assert (await target.flush_session(session_id)).flushed
    manager.attach_conversation("mirror-test", "dm:42", session_id)
    transcript.write_text(old + native_line("user", "next", "after attachment"))
    assert (await target.flush_session(session_id)).flushed
    send.assert_not_awaited()
    live = (
        transcript.read_text()
        + native_line("assistant", "live", "after attachment")
        + native_line("user", "done", "done")
    )
    transcript.write_text(live)
    assert (await target.flush_session(session_id)).flushed
    send.assert_awaited_once()
    rows = manager.store.list_messages(session_id=session_id)
    assert len(rows) == 1 and rows[0].content == "after attachment"


async def test_native_claude_multiline_eof_and_cold_replay_keep_one_delivery(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    tmp_path: Path,
) -> None:
    manager, session_id, send = attached
    lines = [
        {"type": "assistant", "message": {"id": "native-reply", "content": [block]}}
        for block in [
            {"type": "thinking", "thinking": "private"},
            {"type": "text", "text": "First part\n\n"},
            {"type": "text", "text": "Required action: review the whole reply."},
        ]
    ]
    # Missing timestamps exercise parser-synthesized times. No final newline
    # exercises the real EOF reader rather than a synthetic parsed batch.
    content = "\n".join(json.dumps(line) for line in lines)
    transcript = tmp_path / "original.jsonl"
    transcript.write_text(content)
    original = processor(temp_db, manager)
    original.register_session(session_id, str(transcript))
    assert (
        await original.complete_turn(session_id, "Required action: review the whole reply.")
    ).flushed
    assert send.await_count == 1
    row = manager.store.list_messages(session_id=session_id)[0]
    assert row.content == "First part\n\nRequired action: review the whole reply."
    assert row.metadata_json["transcript_message_id"] == f"{session_id}-assistant-native-reply"

    replay = tmp_path / "cold-replay.jsonl"
    replay.write_text(content)
    restarted = processor(temp_db, manager)
    restarted.register_session(session_id, str(replay))
    assert (
        await restarted.complete_turn(session_id, "Required action: review the whole reply.")
    ).flushed
    assert send.await_count == 1
    assert len(manager.store.list_messages(session_id=session_id)) == 1


async def test_stop_drains_inflight_flush_and_rejects_late_work(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
) -> None:
    manager, session_id, send = attached
    target = processor(temp_db, manager)
    target._active_sessions[session_id] = "transcript"
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def finish_pass(*args: object, **kwargs: object) -> bool:
        entered.set()
        await release.wait()
        finished.set()
        return True

    with patch.object(target, "_process_session_unlocked", side_effect=finish_pass) as process:
        flush = asyncio.create_task(target.flush_session(session_id))
        await entered.wait()
        queued_flush = asyncio.create_task(target.flush_session(session_id))
        stopping = asyncio.create_task(target.stop())
        # Give stop its turn while the flush owns the processing lock.
        tick = asyncio.Event()
        asyncio.get_running_loop().call_soon(tick.set)
        await tick.wait()
        try:
            assert not stopping.done()
        finally:
            release.set()
        assert not (await flush).flushed
        assert not (await queued_flush).flushed
        await stopping
        assert finished.is_set()
        late_flush = await target.flush_session(session_id)
        late_completion = await target.complete_turn(session_id, "last")
        assert not late_flush.flushed
        assert not late_completion.flushed
        assert late_flush.error == late_completion.error == "processor is stopped"
        assert process.await_count == 1
    send.assert_not_awaited()


async def test_stop_releases_completion_waiting_for_final_text(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
) -> None:
    manager, session_id, send = attached
    target = processor(temp_db, manager)
    target._active_sessions[session_id] = "transcript"
    entered = asyncio.Event()

    async def no_final_text(*args: object, **kwargs: object) -> bool:
        entered.set()
        return True

    with patch.object(target, "_process_session_unlocked", side_effect=no_final_text) as process:
        completion = asyncio.create_task(target.complete_turn(session_id, "missing"))
        await entered.wait()
        await target.stop()
        result = await completion
    assert not result.flushed
    assert result.error == "processor is stopped"
    assert process.await_count == 1
    send.assert_not_awaited()


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ("Before tool.", "After tool."),
        ("Echo.", "Echo."),
    ],
)
async def test_text_tool_text_waits_for_whole_turn_completion(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    tmp_path: Path,
    resume: bool,
    prefix: str,
    suffix: str,
) -> None:
    manager, session_id, send = attached
    transcript = tmp_path / "tool-turn.jsonl"
    transcript.write_text("")
    target = processor(temp_db, manager)
    target.register_session(session_id, str(transcript))
    records = [
        native_line("assistant", "first", prefix),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "tool",
                    "content": [
                        {"type": "thinking", "thinking": "private reasoning"},
                        {
                            "type": "tool_use",
                            "id": "call",
                            "name": "list_conversation_attachments",
                            "input": {},
                        },
                    ],
                },
            }
        )
        + "\n",
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call", "content": "tool output"},
                    ]
                },
            }
        )
        + "\n",
    ]
    for line in records:
        with transcript.open("a") as stream:
            stream.write(line)
        assert (await target.flush_session(session_id)).flushed
        send.assert_not_awaited()
    if resume:
        target = processor(temp_db, manager)
        target.register_session(session_id, str(transcript))
        assert target._byte_offsets[session_id] == transcript.stat().st_size
    with transcript.open("a") as stream:
        stream.write(native_line("assistant", "last", suffix))
    assert (await target.flush_session(session_id)).flushed
    send.assert_not_awaited()
    assert (await target.complete_turn(session_id, suffix)).flushed
    assert (await target.complete_turn(session_id)).flushed
    send.assert_awaited_once()
    rows = manager.store.list_messages(session_id=session_id)
    assert len(rows) == 1
    assert rows[0].content == prefix + "\n\n" + suffix
    assert rows[0].status == "sent"
    assert send.call_args.args[0].content == rows[0].content


@pytest.mark.parametrize("delivered", [False, True])
async def test_sidecar_restart_keeps_final_reply_and_next_native_response_separate(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    tmp_path: Path,
    delivered: bool,
) -> None:
    manager, session_id, send = attached
    transcript = tmp_path / "resume.jsonl"
    first = json.dumps(
        {
            "type": "assistant",
            "message": {"id": "first", "content": [{"type": "text", "text": "first reply"}]},
        }
    )
    transcript.write_text(first + "\n")
    original = processor(temp_db, manager)
    original.register_session(session_id, str(transcript))
    assert (await original.flush_session(session_id)).flushed
    if delivered:
        assert (await original.complete_turn(session_id, "first reply")).flushed
    restarted = processor(temp_db, manager)
    restarted.register_session(session_id, str(transcript))
    assert restarted._byte_offsets[session_id] == transcript.stat().st_size
    if not delivered:
        assert (await restarted.complete_turn(session_id, "first reply")).flushed
    second = json.dumps(
        {
            "type": "assistant",
            "message": {"id": "second", "content": [{"type": "text", "text": "second reply"}]},
        }
    )
    transcript.write_text(first + "\n" + second + "\n")
    assert (await restarted.complete_turn(session_id, "second reply")).flushed
    assert send.await_count == 2
    assert [call.args[0].content for call in send.call_args_list] == ["first reply", "second reply"]
    assert {row.content for row in manager.store.list_messages(session_id=session_id)} == {
        "first reply",
        "second reply",
    }


async def test_metadata_only_records_keep_fallback_reply_id_stable_across_replay(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    tmp_path: Path,
) -> None:
    manager, session_id, send = attached
    transcript = tmp_path / "indexed.jsonl"
    prefix = (
        "\n".join(
            json.dumps(line)
            for line in [
                {"type": "user", "message": {"content": "prompt"}},
                {"type": "system", "subtype": "unrendered-metadata"},
            ]
        )
        + "\n"
    )
    transcript.write_text(prefix)
    original = processor(temp_db, manager)
    original.register_session(session_id, str(transcript))
    assert (await original.flush_session(session_id)).flushed
    assert original._message_indices[session_id] == 0
    reply = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "reply"}]}}
    )
    transcript.write_text(prefix + reply + "\n")
    assert (await original.complete_turn(session_id, "reply")).flushed
    replay = tmp_path / "indexed-replay.jsonl"
    replay.write_text(transcript.read_text())
    restarted = processor(temp_db, manager)
    restarted.register_session(session_id, str(replay))
    assert (await restarted.complete_turn(session_id, "reply")).flushed
    assert send.await_count == 1
    row = manager.store.list_messages(session_id=session_id)[0]
    assert (
        row.metadata_json["transcript_message_id"]
        == f"{session_id}-assistant-{session_id}:claude:1"
    )


async def test_failed_final_reply_retries_on_poll_without_new_transcript_lines(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    tmp_path: Path,
    retry_clock: Callable[[float], datetime],
) -> None:
    manager, session_id, send = attached
    transcript = tmp_path / "retry.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"id": "reply", "content": [{"type": "text", "text": "whole reply"}]},
            }
        )
        + "\n"
    )
    target = processor(temp_db, manager)
    target.register_session(session_id, str(transcript))
    send.side_effect = [RuntimeError("temporary failure"), "delivered"]
    assert (await target.complete_turn(session_id, "whole reply")).flushed
    retry_clock(10)
    assert (await target.flush_session(session_id)).flushed
    assert send.await_count == 2
    rows = manager.store.list_messages(session_id=session_id)
    assert len(rows) == 1
    assert rows[0].status == "sent"
    assert rows[0].content == "whole reply"


async def test_completed_text_sends_once_after_rollback_and_restart_replay(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
) -> None:
    manager, session_id, send = attached
    batch = [record(0, "assistant", "reply"), record(1, "user", "next")]
    original = processor(temp_db, manager)
    with patch.object(original, "_accumulate_stats", side_effect=RuntimeError("rollback")):
        with pytest.raises(RuntimeError, match="rollback"):
            await original._process_parsed_batch(session_id, batch)
    await original._process_parsed_batch(session_id, batch)
    restored_manager = CommunicationsManager(
        CommunicationsConfig(), manager.store, MagicMock(), SessionManager(temp_db)
    )
    restored_manager._channel_by_name = dict(manager._channel_by_name)
    restored_manager._adapters = dict(manager._adapters)
    restored_manager._restore_telegram_targets()
    restarted = processor(temp_db, restored_manager)
    await restarted._process_parsed_batch(session_id, batch)
    send.assert_awaited_once()
    sent = send.call_args.args[0]
    assert sent.content == "reply"
    assert sent.session_id == session_id
    assert sent.metadata_json["platform_destination"] == "42"
    assert manager.store.get_message(sent.id) is not None
    removed, paths = manager.store.delete_messages_before(TS + timedelta(days=365))
    assert removed == 0
    assert paths == []
    await processor(temp_db, manager)._process_parsed_batch(session_id, batch)
    assert send.await_count == 1


@pytest.mark.parametrize("kind", ["thinking", "tool_use", "partial", "user", "unattached"])
async def test_non_reply_content_is_not_mirrored(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    kind: str,
) -> None:
    manager, session_id, send = attached
    target = processor(temp_db, manager)
    if kind == "unattached":
        manager.detach_conversation("mirror-test", "dm:42", session_id)
    role = "user" if kind == "user" else "assistant"
    content_type = kind if kind in {"thinking", "tool_use"} else "text"
    batch = [record(0, role, "content", content_type)]
    if kind != "partial":
        batch.append(record(1, "user", "next"))
    await target._process_parsed_batch(session_id, batch)
    send.assert_not_awaited()
    assert manager.store.list_messages(session_id=session_id) == []


@pytest.mark.parametrize("expected_text", [None, "final reply"])
async def test_turn_end_waits_for_final_line_and_role_change_does_not_resend(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    expected_text: str | None,
) -> None:
    manager, session_id, send = attached
    target = processor(temp_db, manager)
    target._active_sessions[session_id] = "transcript"
    batches = [
        [record(0, "assistant", "prefix ")],
        [record(1, "assistant", "final reply")],
    ]
    batches[1][0].message_id = batches[0][0].message_id
    first_read = asyncio.Event()
    release_final = asyncio.Event()

    async def read_to_eof(*args: object, **kwargs: object) -> bool:
        if first_read.is_set():
            await release_final.wait()
        batch = batches.pop(0) if batches else []
        await target._process_parsed_batch(session_id, batch)
        first_read.set()
        return True

    with patch.object(target, "_process_session_unlocked", side_effect=read_to_eof):
        completed = asyncio.create_task(target.complete_turn(session_id, expected_text))
        await first_read.wait()
        send.assert_not_awaited()
        release_final.set()
        result = await completed
    assert result.flushed
    send.assert_awaited_once()
    messages = manager.store.list_messages(session_id=session_id)
    assert len(messages) == 1
    assert messages[0].status == "sent"
    assert send.call_args.args[0].content == "prefix final reply"
    await target._process_parsed_batch(session_id, [record(2, "user", "next")])
    assert send.await_count == 1


async def test_failed_delivery_retries_are_bounded(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    retry_clock: Callable[[float], datetime],
) -> None:
    manager, session_id, send = attached
    send.side_effect = RuntimeError("transport down")
    for _ in range(4):
        retry_clock(30)
        with pytest.raises(RuntimeError, match="transport down"):
            await manager.mirror_session_message(session_id, "stable-id", "reply")
    assert send.await_count == 3


@pytest.mark.parametrize("recovery", ["failed", "expired-pending"])
async def test_partial_reply_resumes_only_unconfirmed_chunks(
    attached: tuple[CommunicationsManager, str, AsyncMock],
    recovery: str,
    retry_clock: Callable[[float], datetime],
) -> None:
    manager, session_id, _ = attached
    adapter = TelegramAdapter()
    payloads: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content)["text"])
        if len(payloads) > 1:
            row = manager.store.list_messages(session_id=session_id)[0]
            assert row.metadata_json["platform_message_ids"][0] == "1"
        if len(payloads) == 2:
            if recovery == "expired-pending":
                # The request was observed by the platform, but its response
                # was lost. Reclaiming a crashed lease may duplicate this chunk.
                raise httpx.ReadTimeout("ack lost", request=request)
            return httpx.Response(200, json={"ok": False, "description": "second chunk failed"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(payloads)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter._client = client
        adapter._api_base = "https://telegram.test"
        manager._adapters["mirror-test"] = adapter
        reply = "x" * 9000
        with pytest.raises(RuntimeError, match="second chunk failed|ack lost"):
            await manager.mirror_session_message(session_id, "stable-id", reply)
        row = manager.store.list_messages(session_id=session_id)[0]
        assert row.status == "failed"
        assert row.metadata_json["platform_message_ids"] == ["1"]
        if recovery == "expired-pending":
            row.metadata_json["transcript_lease_until"] = 0
            manager.store.update_message_delivery(row.id, "pending", None, None, row.metadata_json)
        retry_clock(10)
        await manager.mirror_session_message(session_id, "stable-id", reply)
        await manager.mirror_session_message(session_id, "stable-id", reply)
    messages = manager.store.list_messages(session_id=session_id)
    assert len(messages) == 1
    assert messages[0].status == "sent"
    assert messages[0].metadata_json["platform_message_ids"] == ["1", "3", "4"]
    assert payloads[1] == payloads[2]
    delivered = [payload.removeprefix("<b>Claude:</b>\n") for payload in payloads]
    assert delivered[0] + "".join(delivered[2:]) == reply


async def test_concurrent_replay_reserves_only_one_delivery(
    attached: tuple[CommunicationsManager, str, AsyncMock],
) -> None:
    manager, session_id, send = attached
    results = await asyncio.gather(
        *(manager.mirror_session_message(session_id, "stable-id", "reply") for _ in range(3)),
        return_exceptions=True,
    )
    assert all(result is None or isinstance(result, RuntimeError) for result in results)
    send.assert_awaited_once()
    messages = manager.store.list_messages(session_id=session_id)
    assert len(messages) == 1
    assert messages[0].status == "sent"


@pytest.mark.parametrize("length", [4000, 9000])
async def test_mirror_preserves_whole_reply_for_transport_chunking(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
    length: int,
) -> None:
    manager, session_id, send = attached
    content = "x" * length + "\nRequired action: keep this final instruction."
    await processor(temp_db, manager)._process_parsed_batch(
        session_id, [record(0, "assistant", content), record(1, "user", "next")]
    )
    send.assert_awaited_once()
    messages = manager.store.list_messages(session_id=session_id)
    assert len(messages) == 1
    assert messages[0].content == content
    assert send.call_args.args[0].content == content


async def test_consecutive_completed_assistant_turns_have_separate_reservations(
    temp_db: HubDatabase,
    attached: tuple[CommunicationsManager, str, AsyncMock],
) -> None:
    manager, session_id, send = attached
    target = processor(temp_db, manager)
    target._active_sessions[session_id] = "transcript"
    with patch.object(target, "_process_session_unlocked", return_value=True):
        await target._process_parsed_batch(session_id, [record(0, "assistant", "one")])
        assert (await target.complete_turn(session_id, "one")).flushed
        await target._process_parsed_batch(session_id, [record(1, "assistant", "two")])
        assert (await target.complete_turn(session_id, "two")).flushed
    assert send.await_count == 2
    messages = manager.store.list_messages(session_id=session_id)
    assert {message.content for message in messages} == {"one", "two"}
    assert len({message.id for message in messages}) == 2
