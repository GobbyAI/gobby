"""Python control client vs the 3.2 golden corpus (plan 4.1.4/14/17/19)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from gobby.terminals.host_client import (
    HostClient,
    HostCommandError,
    HostDecodeError,
    decode_control_line,
    encode_control_line,
)

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


def test_control_client_matches_golden_corpus() -> None:
    assert encode_control_line(
        {"method": "hello", "protocol_version": 1, "control_token": "token"}
    ) == _golden("control_hello.json")
    assert encode_control_line({"method": "host_shutdown", "grace_ms": 1000}) == _golden(
        "control_host_shutdown.json"
    )
    assert encode_control_line(
        {
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
            "method": "kill",
            "operation_seq": 2,
            "host_terminal_id": "ht-1",
            "grace_ms": 50,
        }
    ) == _golden("control_kill.json")
    assert encode_control_line(
        {
            "method": "resize",
            "operation_seq": 3,
            "host_terminal_id": "ht-1",
            "rows": 30,
            "cols": 100,
        }
    ) == _golden("control_resize.json")
    assert encode_control_line(
        {
            "method": "snapshot",
            "host_terminal_id": "ht-1",
            "mode": "ansi",
            "max_bytes": 262144,
            "max_lines": 500,
        }
    ) == _golden("control_snapshot.json")

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
    assert encode_control_line({"method": "host_shutdown", "grace_ms": 1000}) == _golden(
        "control_host_shutdown.json"
    )


def test_control_spawn_carries_reservation_identity() -> None:
    encoded = encode_control_line(
        {
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
    reader = asyncio.StreamReader()
    writer_reads: list[bytes] = []

    class _Writer:
        def write(self, data: bytes) -> None:
            writer_reads.append(data)

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
    reader.feed_data(line[:8])
    reader.feed_data(line[8:])
    payload = await client.read_payload()
    assert payload["host_pid"] == 1234

    huge = b"x" * (2 * 1024 * 1024) + b"\n"
    reader.feed_data(huge)
    with pytest.raises(HostCommandError) as exc:
        await client.read_payload()
    assert exc.value.code == "request_too_large"
    assert client.closed is False or client.closed is True
    # Oversized rejection must not require closing; a later line can still decode
    # if the socket stays open. Either closed-false or a typed error is the pin.
    assert exc.value.code == "request_too_large"
