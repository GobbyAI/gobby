"""Python frame client vs the 3.2 golden corpus (plan 4.1.8)."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.terminals import AttachLocator
from gobby.terminals.frame_client import (
    FrameClient,
    FrameLagError,
    FrameProtocolError,
    decode_frame,
    encode_frame,
)
from gobby.terminals.host_client import HostEpochChangedError

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


def test_frame_client_matches_golden_corpus() -> None:
    hello = {
        "type": "hello",
        "version": 1,
        "encoding": "semantic_frame",
        "local_token": "local-token",
        "cols": 80,
        "rows": 24,
        "tmux_identity": {
            "socket_path": "/tmp/tmux-sock",
            "server_pid": 9,
            "server_start_time": 1,
            "pane_id": "%0",
        },
    }
    assert encode_frame(hello) == _golden("hello.bin")
    welcome = decode_frame(_golden("welcome.bin"))
    assert welcome["type"] == "welcome"
    assert welcome["host_epoch"] == "epoch-1"

    unreserved = {
        "type": "attach_terminal",
        "host_terminal_id": "ht-1",
        "reservation_id": None,
        "locator": {
            "socket_path": "/tmp/tmux-sock",
            "server_pid": 9,
            "server_start_time": 1,
            "pane_id": "%0",
        },
    }
    assert encode_frame(unreserved) == _golden("attach_terminal.bin")
    reserved = {
        "type": "attach_terminal",
        "host_terminal_id": "ht-1",
        "reservation_id": "rsv-1",
        "locator": None,
    }
    assert encode_frame(reserved) == _golden("attach_terminal_reserved.bin")
    assert encode_frame({"type": "set_viewport", "rows": 24, "cols": 80}) == _golden(
        "set_viewport.bin"
    )
    assert encode_frame({"type": "set_scroll_offset", "rows_from_live_edge": 12}) == _golden(
        "set_scroll_offset.bin"
    )
    assert encode_frame({"type": "detach"}) == _golden("detach.bin")

    for name, expected in (
        ("frame.bin", "frame"),
        ("terminal_ansi.bin", "terminal"),
        ("graphics.bin", "graphics"),
        ("attach_history.bin", "attach_history"),
        ("scroll_offset_applied.bin", "scroll_offset_applied"),
        ("terminal_exited.bin", "terminal_exited"),
    ):
        decoded = decode_frame(_golden(name))
        assert decoded["type"] == expected
    history = decode_frame(_golden("attach_history.bin"))
    assert history["text"] == "history"
    assert history["truncated"] is False

    names = {name for name, _member in inspect.getmembers(FrameClient) if not name.startswith("_")}
    assert "write" not in names
    assert not any("write" == name for name in names)

    oversized = (2 * 1024 * 1024 + 1).to_bytes(4, "little") + b"\x00" * 8
    with pytest.raises(FrameProtocolError):
        decode_frame(oversized)

    mutated = bytearray(_golden("hello.bin"))
    # protocol version field sits after the enum tag in the payload
    mutated[5] = 99
    with pytest.raises(FrameProtocolError):
        decode_frame(bytes(mutated))


@pytest.mark.asyncio
async def test_frame_client_epoch_attach_and_queue() -> None:
    server_reader = asyncio.StreamReader()

    class _CrossWriter:
        def __init__(self, peer: asyncio.StreamReader) -> None:
            self._peer = peer
            self.closed = False

        def write(self, data: bytes) -> None:
            self._peer.feed_data(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                self._peer.feed_eof()

        async def wait_closed(self) -> None:
            return None

        def is_closing(self) -> bool:
            return self.closed

        def get_extra_info(self, name: str, default: object = None) -> object:
            del name
            return default

    incoming = asyncio.StreamReader()
    client = FrameClient(incoming, _CrossWriter(server_reader))
    mismatch = AttachLocator(
        backend="native", frame_host_epoch="epoch-old", host_terminal_id="ht-1"
    )
    incoming.feed_data(_golden("welcome.bin"))
    with pytest.raises(HostEpochChangedError):
        await client.handshake(mismatch)
    assert client.attached is False

    incoming = asyncio.StreamReader()
    server_reader = asyncio.StreamReader()
    client = FrameClient(incoming, _CrossWriter(server_reader))
    locator = AttachLocator(backend="native", frame_host_epoch="epoch-1", host_terminal_id="ht-1")
    incoming.feed_data(_golden("welcome.bin"))
    await client.handshake(locator)
    await client.attach_terminal(locator, reservation_id="rsv-1")
    await client.set_viewport(24, 80)
    await client.set_scroll_offset(12)
    await client.detach()
    assert client.attached is False

    client = FrameClient(asyncio.StreamReader(), _CrossWriter(asyncio.StreamReader()))
    for _ in range(64):
        client.enqueue({"type": "frame"})
    with pytest.raises(FrameLagError):
        client.enqueue({"type": "frame"})

    fragmented = _golden("welcome.bin")
    reader = asyncio.StreamReader()
    writer = _CrossWriter(asyncio.StreamReader())
    client = FrameClient(reader, writer)
    reader.feed_data(fragmented[:3])
    reader.feed_data(fragmented[3:])
    msg = await client.read_message()
    assert msg["type"] == "welcome"


def test_frame_client_has_no_write_method() -> None:
    source = inspect.getsource(FrameClient)
    assert "def write(" not in source
    assert "async def write(" not in source
    _unused: Any = FrameClient
