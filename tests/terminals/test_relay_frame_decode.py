"""Focused relay decoding regressions and throughput coverage."""

from __future__ import annotations

import asyncio
from time import perf_counter_ns
from typing import Any, cast

import pytest

from gobby.servers.websocket.proxy_relay import _map_host_frame
from gobby.terminals.dimensions import MAX_FRAME_SIZE
from gobby.terminals.frame_client import (
    FrameClient,
    FrameProtocolError,
    _bool,
    _bytes,
    _ivarint,
    _string,
    _uvarint,
    decode_frame,
    decode_relay_frame,
)

pytestmark = pytest.mark.unit


class _Writer:
    def write(self, data: bytes) -> None:
        del data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "little") + payload


def _semantic_frame(cell_count: int = 1024) -> bytes:
    cell = b"".join(
        (
            _string("界"),
            _uvarint(300),
            _uvarint(70_000),
            _uvarint(511),
            _bool(False),
            _uvarint(0),
        )
    )
    frame_data = b"".join(
        (
            _uvarint(cell_count),
            cell * cell_count,
            _uvarint(320),
            _uvarint(120),
            _uvarint(0),  # no cursor
            _uvarint(0),  # no hyperlinks
            _bytes(b""),
            b"\x00" * 22,  # zero-valued terminal modes
        )
    )
    return _frame(_uvarint(1) + frame_data)


def test_relay_decoder_preserves_semantic_and_ansi_output() -> None:
    terminal_id = "terminal-1"
    attachment_id = "attachment-1"
    semantic = _semantic_frame(cell_count=300)
    ansi_text = "héλ" * 100
    ansi = _frame(
        b"".join(
            (
                _uvarint(2),
                _uvarint(300),
                _uvarint(320),
                _uvarint(120),
                _bool(True),
                _bytes(ansi_text.encode()),
            )
        )
    )

    for raw, encoding in ((semantic, "semantic_frame"), (ansi, "terminal_ansi")):
        full = _map_host_frame(decode_frame(raw), terminal_id, attachment_id, encoding)
        relay = _map_host_frame(decode_relay_frame(raw), terminal_id, attachment_id, encoding)
        assert relay == full
    assert cast(dict[str, Any], relay)["data"] == ansi_text


@pytest.mark.parametrize(
    "raw",
    [
        _frame(_uvarint(7) + _string("échec" * 60) + _uvarint(1) + _string("ошибка")),
        _frame(_uvarint(6) + _string("端末" * 60) + _uvarint(1) + _ivarint(-300)),
    ],
    ids=["error", "terminal_exited"],
)
def test_relay_decoder_preserves_lifecycle_frames(raw: bytes) -> None:
    assert decode_relay_frame(raw) == decode_frame(raw)


@pytest.mark.asyncio
async def test_relay_reader_preserves_fragmentation_and_cancellation() -> None:
    raw = _frame(
        _uvarint(2)
        + _uvarint(300)
        + _uvarint(320)
        + _uvarint(120)
        + _bool(False)
        + _bytes("こんにちは".encode())
    )
    reader = asyncio.StreamReader()
    client = FrameClient(reader, _Writer())
    reading = asyncio.create_task(client.read_relay_message())
    for offset in range(0, len(raw), 3):
        reader.feed_data(raw[offset : offset + 3])
        await asyncio.sleep(0)
    reader.feed_eof()
    assert await reading == decode_relay_frame(raw)

    blocked_reader = asyncio.StreamReader()
    blocked = asyncio.create_task(FrameClient(blocked_reader, _Writer()).read_relay_message())
    await asyncio.sleep(0)
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked


def test_relay_reader_preserves_frame_errors() -> None:
    with pytest.raises(FrameProtocolError, match="short frame"):
        decode_relay_frame(b"\x00\x00\x00")
    with pytest.raises(FrameProtocolError, match="oversized frame"):
        decode_relay_frame((MAX_FRAME_SIZE + 1).to_bytes(4, "little"))
    with pytest.raises(FrameProtocolError, match="truncated frame"):
        decode_relay_frame((3).to_bytes(4, "little") + b"\x01")


def test_relay_decoder_is_at_least_three_times_faster_for_semantic_frames() -> None:
    raw = _semantic_frame()
    iterations = 100

    decode_frame(raw)
    decode_relay_frame(raw)
    started = perf_counter_ns()
    for _ in range(iterations):
        decode_frame(raw)
    full_ns = perf_counter_ns() - started
    started = perf_counter_ns()
    for _ in range(iterations):
        decode_relay_frame(raw)
    relay_ns = perf_counter_ns() - started

    assert full_ns / relay_ns >= 3, (
        f"relay decoder speedup was {full_ns / relay_ns:.2f}x "
        f"(full={full_ns / iterations:.0f}ns, relay={relay_ns / iterations:.0f}ns)"
    )
