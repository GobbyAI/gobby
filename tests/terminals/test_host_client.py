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


class _LedgerHost:
    """The gterm host's per-connection operation ledger, answered over a fake socket.

    Mirrors ``OperationLedger::decide`` (``crates/gterminal/src/host/ledger.rs``)
    and the single ordered dispatch queue ``handle_connection`` feeds for mutating
    methods (``crates/gterminal/src/host/control.rs``): one decision at a time
    against a monotonic sequence, and a sequence replayed under a different
    request fingerprint is refused ``operation_conflict``.
    """

    _MUTATING = frozenset({"spawn", "kill", "resize", "write", "write_batch"})

    def __init__(self, reader: asyncio.StreamReader, writer: _Writer) -> None:
        self._reader = reader
        self._writer = writer
        self.high_seq = 0
        self.entries: dict[int, tuple[str, dict[str, Any]]] = {}
        self.executed: list[str] = []
        self.errors: list[str] = []
        self.task = asyncio.create_task(self._serve())

    async def aclose(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    @staticmethod
    def _fingerprint(request: dict[str, Any]) -> str:
        extra = {k: v for k, v in request.items() if k not in {"id", "method", "operation_seq"}}
        return json.dumps(extra, sort_keys=True)

    async def _serve(self) -> None:
        try:
            while True:
                request = json.loads(await self._writer.write_queue.get())
                reply = (
                    self._decide(request)
                    if request.get("method") in self._MUTATING
                    else {"ok": True}
                )
                self._reader.feed_data(
                    host_client.encode_control_line({**reply, "id": request["id"]})
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Nothing awaits this task, so a fault here would leave every
            # roundtrip pending forever. EOF fails them and the test reports
            # the fault instead of hanging.
            self._reader.feed_eof()
            raise

    def _decide(self, request: dict[str, Any]) -> dict[str, Any]:
        seq = request["operation_seq"]
        fingerprint = self._fingerprint(request)
        recorded = self.entries.get(seq)
        if recorded is not None:
            if recorded[0] != fingerprint:
                self.errors.append("operation_conflict")
                return {"ok": False, "error": "operation_conflict"}
            return recorded[1]
        if seq > self.high_seq + 1:
            self.errors.append("operation_gap")
            return {"ok": False, "error": "operation_gap"}
        if seq <= self.high_seq:
            self.errors.append("operation_expired")
            return {"ok": False, "error": "operation_expired"}
        outcome: dict[str, Any] = {"ok": True}
        self.entries[seq] = (fingerprint, outcome)
        self.high_seq = seq
        self.executed.append(f"{request['method']}:{seq}")
        return outcome


@pytest.mark.asyncio
async def test_concurrent_mutations_never_claim_the_same_operation_seq() -> None:
    """A second state-changing request waits for the sequence the first claimed.

    ``next_seq`` only advances when the host answers, so two requests built while
    one was in flight used to carry the same sequence, and the host refused the
    loser ``operation_conflict``.
    """
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = HostClient(reader, writer)
    try:
        first = asyncio.create_task(
            client.write(host_terminal_id="ht-1", kind="text", data=b"prompt")
        )
        second = asyncio.create_task(
            client.write(host_terminal_id="ht-2", kind="key", data=b"enter")
        )
        first_request = await writer.next_write()
        assert first_request["operation_seq"] == 1
        # Both tasks have taken their first step by the time the first request
        # reaches the wire, so an empty queue is the second one still waiting.
        assert writer.write_queue.empty(), "second write claimed an unresolved sequence"

        reader.feed_data(host_client.encode_control_line({"ok": True, "id": first_request["id"]}))
        await first
        second_request = await writer.next_write()
        assert second_request["operation_seq"] == 2
        reader.feed_data(host_client.encode_control_line({"ok": True, "id": second_request["id"]}))
        await second
        assert client.next_seq == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_spawn_in_flight_does_not_block_a_snapshot() -> None:
    """Read-only calls stay concurrent; only the ledger's methods serialize.

    The Codex composer readiness poll snapshots while other terminals write, and
    the host dispatches non-mutating methods off its ordered queue.
    """
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = HostClient(reader, writer)
    try:
        spawn = asyncio.create_task(client.spawn(terminal_id="t-1", spawn_key="k-1"))
        spawn_request = await writer.next_write()
        assert spawn_request["operation_seq"] == 1

        snapshot = asyncio.create_task(client.snapshot("ht-1", mode="text"))
        snapshot_request = await writer.next_write()
        assert snapshot_request["method"] == "snapshot"
        assert "operation_seq" not in snapshot_request
        reader.feed_data(
            host_client.encode_control_line(
                {"ok": True, "mode": "text", "text": "› Ask Codex", "id": snapshot_request["id"]}
            )
        )
        assert (await snapshot)["text"] == "› Ask Codex"

        reader.feed_data(host_client.encode_control_line({"ok": True, "id": spawn_request["id"]}))
        await spawn
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_spawn_and_writes_share_the_connection_without_conflicting() -> None:
    """The reported failure: a spawn and other terminals' writes raced the ledger.

    Every caller shares one control connection, so a spawn prompt write, another
    terminal's periodic Enter and a resize all read the same unadvanced
    ``next_seq``; the host executed one and refused the rest.
    """
    reader = asyncio.StreamReader()
    writer = _Writer()
    client = HostClient(reader, writer)
    host = _LedgerHost(reader, writer)
    try:
        results = await asyncio.gather(
            client.spawn(terminal_id="t-1", spawn_key="k-1", argv=["codex"]),
            client.write(host_terminal_id="ht-1", kind="text", data=b"prompt", submit=False),
            client.write(host_terminal_id="ht-2", kind="key", data=b"enter"),
            client.resize("ht-3", 24, 80),
            return_exceptions=True,
        )
        assert [result for result in results if isinstance(result, BaseException)] == []
        assert host.errors == []
        assert host.executed == ["spawn:1", "write:2", "write:3", "resize:4"]
        assert client.next_seq == 5
    finally:
        await host.aclose()
        await client.close()
