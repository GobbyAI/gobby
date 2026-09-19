"""Typed gterm control-client failure behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from gobby.terminals import host_client
from gobby.terminals.host_client import (
    CommitTransportError,
    HostClient,
    HostCommandError,
    HostUnavailableError,
)

pytestmark = pytest.mark.unit


class _Writer:
    def __init__(self, *, write_error: Exception | None = None) -> None:
        self.write_error = write_error
        self.writes: list[bytes] = []
        self.write_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False

    def write(self, data: bytes) -> object:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(data)
        self.write_queue.put_nowait(data)
        return None

    async def drain(self) -> object:
        return None

    def close(self) -> object:
        self.closed = True
        return None

    async def wait_closed(self) -> object:
        return None

    async def next_write(self) -> dict[str, object]:
        decoded: object = json.loads(await asyncio.wait_for(self.write_queue.get(), timeout=1.0))
        assert isinstance(decoded, dict)
        return {str(key): value for key, value in decoded.items()}


@pytest.mark.asyncio
async def test_reader_task_correlates_and_fails_pending_on_loss() -> None:
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = HostClient(reader, writer)
    first = asyncio.create_task(client._roundtrip({"method": "first"}))
    second = asyncio.create_task(client._roundtrip({"method": "second"}))
    tasks = [first, second]

    try:
        await writer.next_write()
        await writer.next_write()
        assert len(writer.writes) == 2
        requests = [json.loads(line) for line in writer.writes]
        assert requests[0]["id"] != requests[1]["id"]

        reader.feed_data(
            host_client.encode_control_line(
                {"ok": True, "id": requests[1]["id"], "result": "second"}
            )
        )
        reader.feed_data(
            host_client.encode_control_line(
                {"ok": True, "id": requests[0]["id"], "result": "first"}
            )
        )
        assert (await first)["result"] == "first"
        assert (await second)["result"] == "second"

        lost_one = asyncio.create_task(client._roundtrip({"method": "lost-one"}))
        lost_two = asyncio.create_task(client._roundtrip({"method": "lost-two"}))
        tasks.extend((lost_one, lost_two))
        await writer.next_write()
        await writer.next_write()
        reader.feed_eof()
        with pytest.raises(host_client.HostConnectionLost):
            await lost_one
        with pytest.raises(host_client.HostConnectionLost):
            await lost_two
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


@pytest.mark.asyncio
async def test_reconnect_replaces_reader_generation_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old_reader = asyncio.StreamReader(limit=host_client.MAX_CONTROL_LINE + 1)
    old_writer = _Writer()
    client = HostClient(old_reader, old_writer)

    hello_task = asyncio.create_task(client.hello(host_client.CONTROL_PROTOCOL_VERSION, "token"))
    hello_request = await old_writer.next_write()
    old_reader.feed_data(
        host_client.encode_control_line(
            {
                "id": hello_request["id"],
                "ok": True,
                "host_epoch": "epoch-1",
                "version": "test",
                "protocol_version": host_client.CONTROL_PROTOCOL_VERSION,
            }
        )
    )
    await hello_task

    pending = asyncio.create_task(client._roundtrip({"method": "old-generation"}))
    await old_writer.next_write()
    old_reader_task = client._reader_task

    new_reader = asyncio.StreamReader(limit=host_client.MAX_CONTROL_LINE + 1)
    new_writer = _Writer()

    async def open_connection(*, path: str, limit: int) -> tuple[asyncio.StreamReader, _Writer]:
        assert path == str(tmp_path / "control.sock")
        assert limit == host_client.MAX_CONTROL_LINE + 1
        return new_reader, new_writer

    monkeypatch.setattr(asyncio, "open_unix_connection", open_connection)
    reconnect = asyncio.create_task(client.reconnect(tmp_path / "control.sock", "epoch-1"))

    with pytest.raises(host_client.HostConnectionLost):
        await pending
    hello_request = await new_writer.next_write()
    new_reader.feed_data(
        host_client.encode_control_line(
            {
                "id": hello_request["id"],
                "ok": True,
                "host_epoch": "epoch-1",
                "version": "test",
                "protocol_version": host_client.CONTROL_PROTOCOL_VERSION,
            }
        )
    )
    ping_request = await new_writer.next_write()
    new_reader.feed_data(
        host_client.encode_control_line(
            {
                "id": ping_request["id"],
                "ok": True,
                "host_epoch": "epoch-1",
                "version": "test",
                "host_pid": 42,
            }
        )
    )
    assert await reconnect == "epoch-1"
    assert old_reader_task is not None and old_reader_task.done()
    assert old_writer.closed is True

    current = asyncio.create_task(client._roundtrip({"method": "current-generation"}))
    current_request = await new_writer.next_write()
    old_reader.feed_data(
        host_client.encode_control_line({"id": current_request["id"], "ok": True, "stale": True})
    )
    new_reader.feed_data(
        host_client.encode_control_line({"id": current_request["id"], "ok": True, "current": True})
    )
    assert (await current)["current"] is True

    cancelled = asyncio.create_task(client._roundtrip({"method": "cancelled-caller"}))
    cancelled_request = await new_writer.next_write()
    survivor = asyncio.create_task(client._roundtrip({"method": "surviving-caller"}))
    survivor_request = await new_writer.next_write()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert str(cancelled_request["id"]) not in client._pending
    new_reader.feed_data(
        host_client.encode_control_line(
            {"id": survivor_request["id"], "ok": True, "survived": True}
        )
    )
    new_reader.feed_data(
        host_client.encode_control_line({"id": cancelled_request["id"], "ok": True, "late": True})
    )
    assert (await survivor)["survived"] is True
    await client.close()


@pytest.mark.asyncio
async def test_reader_decodes_large_reply_and_fails_over_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    connections = [
        (asyncio.StreamReader(limit=host_client.MAX_CONTROL_LINE + 1), _Writer()) for _ in range(3)
    ]

    async def open_connection(*, path: str, limit: int) -> tuple[asyncio.StreamReader, _Writer]:
        assert path == str(tmp_path / "control.sock")
        assert limit == host_client.MAX_CONTROL_LINE + 1
        return connections.pop(0)

    def one_mebibyte_reply(payload: dict[str, Any]) -> bytes:
        fixed = host_client.encode_control_line({**payload, "blob": ""})
        encoded = host_client.encode_control_line(
            {**payload, "blob": "x" * (1024 * 1024 - len(fixed))}
        )
        assert len(encoded) == 1024 * 1024
        return encoded

    monkeypatch.setattr(asyncio, "open_unix_connection", open_connection)
    initial_reader, initial_writer = connections[0]
    client = await HostClient.connect(tmp_path / "control.sock")
    initial = asyncio.create_task(client._roundtrip({"method": "initial-large"}))
    initial_request = await initial_writer.next_write()
    initial_reader.feed_data(one_mebibyte_reply({"id": initial_request["id"], "ok": True}))
    assert len((await initial)["blob"]) > 1_000_000

    replacement_reader, replacement_writer = connections[0]
    reconnect = asyncio.create_task(client.reconnect(tmp_path / "control.sock"))
    ping_request = await replacement_writer.next_write()
    replacement_reader.feed_data(
        one_mebibyte_reply(
            {
                "id": ping_request["id"],
                "ok": True,
                "host_epoch": "epoch-large",
                "host_pid": 42,
                "version": "test",
            }
        )
    )
    assert await reconnect == "epoch-large"

    event_reader, event_writer = connections[0]
    opening_stream = asyncio.create_task(
        HostClient.open_event_stream(tmp_path / "control.sock", "token")
    )
    hello_request = await event_writer.next_write()
    event_reader.feed_data(
        host_client.encode_control_line(
            {
                "id": hello_request["id"],
                "ok": True,
                "host_epoch": "epoch-large",
                "protocol_version": host_client.CONTROL_PROTOCOL_VERSION,
                "version": "test",
            }
        )
    )
    subscribe_request = await event_writer.next_write()
    event_reader.feed_data(
        host_client.encode_control_line(
            {
                "id": subscribe_request["id"],
                "ok": True,
                "epoch": "epoch-large",
                "seq": 0,
                "gap": False,
            }
        )
    )
    event_stream = await opening_stream
    event_reader.feed_data(
        one_mebibyte_reply(
            {
                "event": "terminal_exited",
                "terminal_id": "terminal-1",
                "host_terminal_id": "host-terminal-1",
                "exit_code": 0,
                "epoch": "epoch-large",
                "seq": 1,
            }
        )
    )
    assert (await anext(event_stream)).seq == 1
    await event_stream.aclose()

    over_one = asyncio.create_task(client._roundtrip({"method": "over-one"}))
    over_two = asyncio.create_task(client._roundtrip({"method": "over-two"}))
    over_request = await replacement_writer.next_write()
    await replacement_writer.next_write()
    fixed = host_client.encode_control_line({"id": over_request["id"], "ok": True, "blob": ""})
    encoded = host_client.encode_control_line(
        {
            "id": over_request["id"],
            "ok": True,
            "blob": "x" * (host_client.MAX_CONTROL_LINE - len(fixed) + 1),
        }
    )
    assert len(encoded) == host_client.MAX_CONTROL_LINE + 1
    replacement_reader.feed_data(encoded)
    with pytest.raises(host_client.HostConnectionLost):
        await over_one
    with pytest.raises(host_client.HostConnectionLost):
        await over_two
    assert client.closed is True
    await client.close()


@pytest.mark.asyncio
async def test_commit_transport_error_reports_written_state() -> None:
    write_client = HostClient(
        asyncio.StreamReader(),
        _Writer(write_error=BrokenPipeError("write failed")),
    )
    with pytest.raises(CommitTransportError) as before_write:
        await write_client.spawn_commit("terminal-1", "spawn-1", 30_000)
    assert before_write.value.request_written is False

    reader = asyncio.StreamReader()
    read_writer = _Writer()
    read_client = HostClient(reader, read_writer)
    commit = asyncio.create_task(read_client.spawn_commit("terminal-2", "spawn-2", 30_000))
    request = await read_writer.next_write()
    assert request["method"] == "spawn_commit"
    reader.feed_eof()
    with pytest.raises(CommitTransportError) as after_write:
        await commit
    assert after_write.value.request_written is True
    assert b'"commit_deadline_ms":30000' in read_writer.writes[0]

    other_reader = asyncio.StreamReader()
    other_reader.feed_eof()
    with pytest.raises(HostUnavailableError):
        await HostClient(other_reader, _Writer()).ping()


def test_raise_for_payload_preserves_structured_error() -> None:
    with pytest.raises(HostCommandError) as raised:
        HostClient.raise_for_payload(
            {
                "ok": False,
                "error": "exec_failed",
                "code": "ENOENT",
                "detail": "No such file or directory",
                "stage": "exec",
            }
        )

    assert raised.value.error == "exec_failed"
    assert raised.value.code == "ENOENT"
    assert raised.value.detail == "No such file or directory"
    assert raised.value.stage == "exec"


async def test_ledger_error_advances_seq_so_kill_does_not_conflict() -> None:
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = HostClient(reader, writer)
    try:
        write_task = asyncio.create_task(
            client.write(host_terminal_id="ht-1", kind="text", data=b"ECHO gone")
        )
        write_req = await writer.next_write()
        assert write_req["operation_seq"] == 1
        reader.feed_data(
            host_client.encode_control_line(
                {"ok": False, "error": "not_found", "id": write_req["id"]}
            )
        )
        with pytest.raises(HostCommandError) as raised:
            await write_task
        assert raised.value.error == "not_found"
        assert client.next_seq == 2

        kill_task = asyncio.create_task(client.kill("ht-1", grace_ms=200))
        kill_req = await writer.next_write()
        assert kill_req["method"] == "kill"
        assert kill_req["operation_seq"] == 2
        reader.feed_data(
            host_client.encode_control_line({"ok": True, "killed": False, "id": kill_req["id"]})
        )
        await kill_task
        assert client.next_seq == 3
    finally:
        await client.close()


async def test_operation_gap_does_not_advance_seq() -> None:
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = HostClient(reader, writer)
    try:
        write_task = asyncio.create_task(
            client.write(host_terminal_id="ht-1", kind="text", data=b"x")
        )
        write_req = await writer.next_write()
        reader.feed_data(
            host_client.encode_control_line(
                {"ok": False, "error": "operation_gap", "id": write_req["id"]}
            )
        )
        with pytest.raises(HostCommandError) as raised:
            await write_task
        assert raised.value.error == "operation_gap"
        assert client.next_seq == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_snapshot_requires_the_host_to_echo_the_requested_mode() -> None:
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = HostClient(reader, writer)
    try:
        for reply, answered in (
            ({"ok": True, "mode": "ansi", "text": "\x1b[31mred"}, "'ansi'"),
            ({"ok": True, "text": "red"}, "None"),
        ):
            task = asyncio.create_task(client.snapshot("ht-1", mode="text"))
            request = await writer.next_write()
            assert request["mode"] == "text"
            reader.feed_data(host_client.encode_control_line(reply | {"id": request["id"]}))
            with pytest.raises(HostCommandError) as refused:
                await task
            assert refused.value.error == "snapshot_mode_mismatch"
            assert refused.value.detail is not None
            assert answered in refused.value.detail

        task = asyncio.create_task(client.snapshot("ht-1", mode="ansi"))
        request = await writer.next_write()
        assert request["mode"] == "ansi"
        reader.feed_data(
            host_client.encode_control_line(
                {"ok": True, "mode": "ansi", "text": "\x1b[31mred", "id": request["id"]}
            )
        )
        assert (await task)["text"] == "\x1b[31mred"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_mutating_requests_never_share_an_operation_seq() -> None:
    """Overlapping writes must not both take the sequence the host executes once.

    The sequence space is per connection while the write coordinator locks per
    terminal, so two terminals writing at once used to read the same ``next_seq``
    and the host refused the loser ``operation_conflict``.
    """
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = HostClient(reader, writer)
    try:
        first = asyncio.create_task(
            client.write(host_terminal_id="ht-1", kind="key", data=b"enter")
        )
        first_req = await writer.next_write()
        assert first_req["operation_seq"] == 1

        second = asyncio.create_task(client.resize("ht-2", rows=40, cols=120))
        await asyncio.sleep(0)
        # The second request waits for the first reply rather than pipelining
        # behind it against a sequence the host has not recorded yet.
        assert writer.write_queue.empty()

        reader.feed_data(
            host_client.encode_control_line({"ok": True, "written": True, "id": first_req["id"]})
        )
        await first
        assert client.next_seq == 2

        second_req = await writer.next_write()
        assert second_req["method"] == "resize"
        assert second_req["operation_seq"] == 2
        reader.feed_data(host_client.encode_control_line({"ok": True, "id": second_req["id"]}))
        await second
        assert client.next_seq == 3
    finally:
        await client.close()
