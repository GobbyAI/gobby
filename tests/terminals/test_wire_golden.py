"""Python control client vs the 3.2 golden corpus (plan 4.1.4/14/17/19)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from gobby.terminals.host_client import (
    MAX_CONTROL_LINE,
    MAX_WRITE_BATCH_OPERATIONS_PER_TARGET,
    MAX_WRITE_BATCH_TARGETS,
    HostBatchOperation,
    HostBatchTarget,
    HostClient,
    HostCommandError,
    HostConnectionLost,
    HostDecodeError,
    decode_control_line,
    encode_control_line,
)
from gobby.terminals.host_control import HostControlClient

pytestmark = pytest.mark.unit

GOLDEN = (
    Path(__file__).resolve().parents[2]
    / "crates"
    / "gterminal"
    / "tests"
    / "fixtures"
    / "wire_golden"
)


def _golden(name: str) -> bytes:
    return (GOLDEN / name).read_bytes()


def test_control_goldens_match_rust_emitter() -> None:
    request_ids = {
        "control_hello.json": "hello-1",
        "control_host_shutdown.json": "shutdown-1",
        "control_spawn.json": "spawn-1",
        "control_spawn_commit.json": "commit-1",
        "control_kill.json": "kill-1",
        "control_resize.json": "resize-1",
        "control_snapshot.json": "snapshot-1",
        "control_snapshot_text.json": "snapshot-2",
        "control_snapshot_result.json": "snapshot-2",
        "control_write.json": "write-1",
        "control_write_batch.json": "batch-1",
        "control_write_paste_on.json": "paste-on-1",
        "control_write_paste_off.json": "paste-off-1",
        "control_subscribe_events.json": "subscribe-1",
        "control_reserve_observer.json": "reserve-1",
        "control_release_observer.json": "release-1",
    }
    for name, request_id in request_ids.items():
        assert decode_control_line(_golden(name))["id"] == request_id

    assert decode_control_line(_golden("control_subscribe_events.json"))["since"] == 41
    assert decode_control_line(_golden("control_ping.json"))["id"] == "ping-1"
    assert decode_control_line(_golden("control_list.json"))["id"] == "list-1"
    assert decode_control_line(_golden("control_spawn_prepared.json"))["id"] == "spawn-1"
    terminal_exited = decode_control_line(_golden("control_terminal_exited.json"))
    assert terminal_exited == {
        "event": "terminal_exited",
        "terminal_id": "t",
        "host_terminal_id": "ht-1",
        "exit_code": 7,
        "epoch": "epoch-1",
        "seq": 42,
    }


def test_control_client_matches_golden_corpus() -> None:
    assert encode_control_line(
        {
            "id": "hello-1",
            "method": "hello",
            "protocol_version": 1,
            "control_token": "token",
        }
    ) == _golden("control_hello.json")
    assert encode_control_line(
        {"id": "shutdown-1", "method": "host_shutdown", "grace_ms": 1000}
    ) == _golden("control_host_shutdown.json")
    assert encode_control_line(
        {
            "id": "spawn-1",
            "method": "spawn",
            "operation_seq": 1,
            "terminal_id": "t",
            "spawn_key": "s",
            "reservation_id": "rsv",
            "reserve_key": "rk",
            "argv": ["/bin/sh"],
            "env": {},
            "cwd": "/tmp",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 30000,
        }
    ) == _golden("control_spawn.json")
    assert encode_control_line(
        {
            "id": "write-1",
            "method": "write",
            "operation_seq": 4,
            "host_terminal_id": "ht-1",
            "kind": "text",
            "encoding": "utf8-b64",
            "data": "eA==",
            "submit": False,
        }
    ) == _golden("control_write.json")
    assert encode_control_line(
        {
            "id": "batch-1",
            "method": "write_batch",
            "operation_seq": 7,
            "targets": [
                {
                    "recipient_id": "r1",
                    "host_terminal_id": "ht-1",
                    "operations": [
                        {
                            "kind": "text",
                            "encoding": "utf8-b64",
                            "data": "eA==",
                            "delay_ms": 0,
                        }
                    ],
                },
                {
                    "recipient_id": "r2",
                    "host_terminal_id": "ht-2",
                    "operations": [
                        {
                            "kind": "key",
                            "encoding": "utf8-b64",
                            "data": "ZW50ZXI=",
                            "delay_ms": 15,
                        }
                    ],
                },
                {
                    "recipient_id": "r3",
                    "host_terminal_id": "ht-3",
                    "operations": [
                        {
                            "kind": "text",
                            "encoding": "utf8-b64",
                            "data": "eQ==",
                            "delay_ms": 0,
                        }
                    ],
                },
            ],
        }
    ) == _golden("control_write_batch.json")
    assert encode_control_line(
        {
            "id": "kill-1",
            "method": "kill",
            "operation_seq": 2,
            "host_terminal_id": "ht-1",
            "grace_ms": 50,
        }
    ) == _golden("control_kill.json")
    assert encode_control_line(
        {
            "id": "resize-1",
            "method": "resize",
            "operation_seq": 3,
            "host_terminal_id": "ht-1",
            "rows": 30,
            "cols": 100,
        }
    ) == _golden("control_resize.json")
    assert encode_control_line(
        {
            "id": "snapshot-1",
            "method": "snapshot",
            "host_terminal_id": "ht-1",
            "mode": "ansi",
            "max_bytes": 262144,
            "max_lines": 500,
        }
    ) == _golden("control_snapshot.json")
    assert encode_control_line(
        {
            "id": "snapshot-2",
            "method": "snapshot",
            "host_terminal_id": "ht-1",
            "mode": "text",
            "max_bytes": 262144,
            "max_lines": 500,
        }
    ) == _golden("control_snapshot_text.json")
    snapshot_result = decode_control_line(_golden("control_snapshot_result.json"))
    assert snapshot_result["ok"] is True
    assert snapshot_result["mode"] == "text"
    assert snapshot_result["text"] == "one\ntwo"
    assert snapshot_result["truncated"] is True
    assert snapshot_result["dropped_bytes"] == 4
    assert snapshot_result["total_bytes"] == 12

    ping = decode_control_line(_golden("control_ping.json"))
    assert ping["host_pid"] == 1234
    assert ping["host_epoch"] == "epoch-1"
    listed = decode_control_line(_golden("control_list.json"))
    assert listed["ok"] is True
    prepared = decode_control_line(_golden("control_spawn_prepared.json"))
    assert prepared["reservation_id"] == "rsv"
    assert prepared["reserve_generation"] == 1

    with pytest.raises(HostCommandError) as mismatch:
        HostClient.raise_for_payload({"ok": False, "error": "unsupported_protocol"})
    assert mismatch.value.code == "unsupported_protocol"

    with pytest.raises(HostCommandError) as oversized:
        HostClient.raise_for_payload({"ok": False, "error": "request_too_large"})
    assert oversized.value.code == "request_too_large"


def test_control_ping_requires_host_pid() -> None:
    payload = decode_control_line(_golden("control_ping.json"))
    assert payload["host_pid"] == 1234
    HostClient.require_ping(payload)
    with pytest.raises(HostDecodeError):
        HostClient.require_ping({"ok": True, "host_epoch": "epoch-1", "version": "0.1.0"})


def test_control_host_shutdown_round_trip() -> None:
    assert encode_control_line(
        {"id": "shutdown-1", "method": "host_shutdown", "grace_ms": 1000}
    ) == _golden("control_host_shutdown.json")


@pytest.mark.asyncio
async def test_legacy_control_client_adds_unique_request_ids() -> None:
    reader = asyncio.StreamReader()
    writes: asyncio.Queue[bytes] = asyncio.Queue()

    class _Writer:
        def write(self, data: bytes) -> None:
            writes.put_nowait(data)

        async def drain(self) -> None:
            return None

    client = HostControlClient(reader, cast(asyncio.StreamWriter, _Writer()))
    request_ids: list[object] = []
    for _ in range(2):
        ping_task = asyncio.create_task(client.ping())
        request = decode_control_line(await writes.get())
        request_ids.append(request.get("id"))
        reader.feed_data(
            encode_control_line(
                {
                    "id": request.get("id"),
                    "ok": True,
                    "host_epoch": "epoch-1",
                    "version": "0.1.0",
                    "host_pid": 1234,
                }
            )
        )
        await ping_task

    assert all(isinstance(request_id, str) for request_id in request_ids)
    assert len(set(request_ids)) == 2


def test_control_spawn_carries_reservation_identity() -> None:
    encoded = encode_control_line(
        {
            "id": "spawn-1",
            "method": "spawn",
            "operation_seq": 1,
            "terminal_id": "t",
            "spawn_key": "s",
            "reservation_id": "rsv",
            "reserve_key": "rk",
            "argv": ["/bin/sh"],
            "env": {},
            "cwd": "/tmp",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 30000,
        }
    )
    assert encoded == _golden("control_spawn.json")
    prepared = decode_control_line(_golden("control_spawn_prepared.json"))
    assert prepared["reservation_id"] == "rsv"
    assert prepared["reserve_key"] == "rk"
    assert prepared["reserve_generation"] == 1
    with pytest.raises(HostCommandError) as missing:
        HostClient.raise_for_payload({"ok": False, "error": "invalid_reservation"})
    assert missing.value.code == "invalid_reservation"


@pytest.mark.asyncio
async def test_control_client_fragmented_and_oversized_reads() -> None:
    reader = asyncio.StreamReader(limit=MAX_CONTROL_LINE + 1)
    writer_reads: list[bytes] = []
    writes: asyncio.Queue[bytes] = asyncio.Queue()

    class _Writer:
        def write(self, data: bytes) -> None:
            writer_reads.append(data)
            writes.put_nowait(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

        def is_closing(self) -> bool:
            return False

        def get_extra_info(self, name: str, default: object = None) -> object:
            del name
            return default

    client = HostClient(reader, _Writer())
    line = _golden("control_ping.json")
    ping_task = asyncio.create_task(client._roundtrip({"method": "ping", "id": "ping-1"}))
    await writes.get()
    reader.feed_data(line[:8])
    reader.feed_data(line[8:])
    payload = await ping_task
    assert payload["host_pid"] == 1234

    oversized_task = asyncio.create_task(client._roundtrip({"method": "oversized"}))
    await writes.get()
    huge = b"x" * (2 * 1024 * 1024) + b"\n"
    reader.feed_data(huge)
    with pytest.raises(HostConnectionLost) as exc:
        await oversized_task
    assert exc.value.code == "host_unavailable"
    await client.close()


@pytest.mark.asyncio
async def test_write_batch_uses_one_locked_round_trip_for_three_recipients() -> None:
    reader = asyncio.StreamReader()
    written: list[bytes] = []
    writes: asyncio.Queue[bytes] = asyncio.Queue()

    class _Writer:
        def write(self, data: bytes) -> None:
            written.append(data)
            writes.put_nowait(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    client = HostClient(reader, _Writer())
    targets = tuple(
        HostBatchTarget(
            recipient_id=f"r{index}",
            host_terminal_id=f"ht-{index}",
            operations=(
                HostBatchOperation(kind="text", data=b"wake", delay_ms=1000),
                HostBatchOperation(kind="key", data=b"enter", delay_ms=1000),
            ),
        )
        for index in range(1, 4)
    )

    ping_task = asyncio.create_task(client.ping())
    ping_request = decode_control_line(await writes.get())
    batch_task = asyncio.create_task(client.write_batch(targets))
    request = decode_control_line(await writes.get())
    assert len(written) == 2
    assert request["method"] == "write_batch"
    assert [item["recipient_id"] for item in request["targets"]] == ["r1", "r2", "r3"]
    assert all(len(item["operations"]) == 2 for item in request["targets"])

    reader.feed_data(
        encode_control_line(
            {
                "id": request["id"],
                "ok": True,
                "results": [
                    {
                        "recipient_id": f"r{index}",
                        "host_terminal_id": f"ht-{index}",
                        "ok": True,
                        "written": True,
                    }
                    for index in range(1, 4)
                ],
            }
        )
    )
    results = await batch_task
    assert [item["recipient_id"] for item in results] == ["r1", "r2", "r3"]
    assert not ping_task.done()
    reader.feed_data(
        encode_control_line(
            {
                "id": ping_request["id"],
                "host_epoch": "epoch-1",
                "host_pid": 1234,
                "ok": True,
                "version": "0.1.0",
            }
        )
    )
    await ping_task
    await client.close()


@pytest.mark.asyncio
async def test_write_batch_rejects_limits_before_socket_io() -> None:
    reader = asyncio.StreamReader()
    written: list[bytes] = []

    class _Writer:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    client = HostClient(reader, _Writer())
    operation = HostBatchOperation(kind="text", data=b"x", delay_ms=0)
    target = HostBatchTarget("r", "ht", (operation,))

    with pytest.raises(HostCommandError) as targets_error:
        await client.write_batch([target] * (MAX_WRITE_BATCH_TARGETS + 1))
    assert targets_error.value.code == "too_many_targets"
    with pytest.raises(HostCommandError) as operations_error:
        await client.write_batch(
            [
                HostBatchTarget(
                    "r",
                    "ht",
                    (operation,) * (MAX_WRITE_BATCH_OPERATIONS_PER_TARGET + 1),
                )
            ]
        )
    assert operations_error.value.code == "too_many_operations"
    with pytest.raises(HostCommandError) as delay_error:
        await client.write_batch(
            [HostBatchTarget("r", "ht", (HostBatchOperation("text", b"x", 1001),))]
        )
    assert delay_error.value.code == "invalid_delay"
    with pytest.raises(HostCommandError) as total_delay_error:
        await client.write_batch(
            [
                HostBatchTarget(
                    "r",
                    "ht",
                    tuple(HostBatchOperation("text", b"x", 1000) for _ in range(6)),
                )
            ]
        )
    assert total_delay_error.value.code == "invalid_delay"
    assert written == []
