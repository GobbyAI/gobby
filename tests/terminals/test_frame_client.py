"""Python frame client vs the 3.2 golden corpus (plan 4.1.8)."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.storage.terminals import AttachLocator
from gobby.terminals.frame_client import (
    FrameClient,
    FrameLagError,
    FrameProtocolError,
    decode_frame,
    encode_frame,
)
from gobby.terminals.host_client import HostEpochChangedError, HostNotAdoptedError

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


def test_bincode_varint_encodes_values_above_127() -> None:
    payload = {
        "type": "attach_terminal",
        "host_terminal_id": "ht-1",
        "reservation_id": None,
        "locator": {
            "socket_path": "/tmp/tmux-sock",
            "server_pid": 18789,
            "server_start_time": 1787291792,
            "pane_id": "%0",
        },
    }
    decoded = decode_frame(encode_frame(payload))
    assert decoded["locator"]["server_pid"] == 18789
    assert decoded["locator"]["server_start_time"] == 1787291792
    # Bincode-2 single-byte varints go up to 250; protobuf uleb128 would split at 128.
    small = encode_frame(
        {
            "type": "attach_terminal",
            "host_terminal_id": "ht-1",
            "reservation_id": None,
            "locator": {
                "socket_path": "/tmp/tmux-sock",
                "server_pid": 64,
                "server_start_time": 1,
                "pane_id": "%0",
            },
        }
    )
    assert b"\x80\x01" not in small


@pytest.mark.asyncio
async def test_tmux_attach_requires_generation() -> None:
    incoming = asyncio.StreamReader()
    outgoing = asyncio.StreamReader()

    class _Writer:
        def write(self, data: bytes) -> None:
            outgoing.feed_data(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    client = FrameClient(incoming, _Writer())
    locator = AttachLocator(
        backend="tmux",
        frame_host_epoch="epoch-1",
        socket_path="/tmp/tmux.sock",
        pane_id="%0",
    )
    with pytest.raises(FrameProtocolError, match="generation"):
        await client.attach_terminal(locator)


def test_frame_client_has_no_write_method() -> None:
    source = inspect.getsource(FrameClient)
    assert "def write(" not in source
    assert "async def write(" not in source
    _unused: Any = FrameClient


async def test_default_handshake_reads_bootstrap_key_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gobby_home = tmp_path / "gobby-home"
    gobby_home.mkdir()
    bootstrap = gobby_home / "bootstrap.yaml"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    monkeypatch.setenv("GOBBY_AGENT_API_TOKEN", "managed-capability")
    monkeypatch.setattr("gobby.utils.local_token._daemon_bootstrap", None)
    welcome = _golden("welcome.bin")
    locator = AttachLocator(
        backend="native",
        frame_host_epoch=str(decode_frame(welcome)["host_epoch"]),
        host_terminal_id="ht-1",
    )

    for document, expected in (
        ("api_key: ' first-key '\n", "first-key"),
        ("api_key: rotated-key\n", "rotated-key"),
        ("api_key: ''\n", ""),
        ("api_key: 123\n", ""),
        ("api_key: [broken\n", ""),
        (None, ""),
    ):
        if document is None:
            bootstrap.unlink()
        else:
            bootstrap.write_text(document, encoding="utf-8")
        incoming = asyncio.StreamReader()
        incoming.feed_data(welcome)
        writer = _RecordingWriter()
        client = FrameClient(incoming, writer)
        await client.handshake(locator)
        assert writer.writes == [
            encode_frame(
                {
                    "type": "hello",
                    "version": 1,
                    "encoding": "semantic_frame",
                    "local_token": expected,
                    "cols": 80,
                    "rows": 24,
                    "tmux_identity": None,
                }
            )
        ]
        await client.close()


async def test_handshake_failure_names_the_host_error_code() -> None:
    class _Writer:
        def write(self, data: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    incoming = asyncio.StreamReader()
    incoming.feed_data(_golden("error_stale.bin"))
    client = FrameClient(incoming, cast(Any, _Writer()))
    locator = AttachLocator(backend="native", frame_host_epoch="epoch-1", host_terminal_id="ht-1")

    with pytest.raises(FrameProtocolError) as failure:
        await client.handshake(locator, local_token="token")

    assert "stale" in str(failure.value)


class _RecordingWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def test_handshake_without_adopted_epoch_reports_not_adopted() -> None:
    """No adopted epoch is reported as such, before any hello is sent (#22337)."""
    incoming = asyncio.StreamReader()
    incoming.feed_data(_golden("welcome.bin"))
    writer = _RecordingWriter()
    client = FrameClient(incoming, cast(Any, writer))
    locator = AttachLocator(backend="native", frame_host_epoch="", host_terminal_id="ht-1")

    with pytest.raises(HostNotAdoptedError) as failure:
        await client.handshake(locator, local_token="token")

    assert not isinstance(failure.value, HostEpochChangedError)
    assert str(failure.value) == "host_not_adopted"
    assert failure.value.detail == "gterm host not adopted"
    assert writer.writes == [], "nothing was sent to the host"
    assert writer.closed is True
    assert client.attached is False


async def test_handshake_with_changed_epoch_still_raises_host_epoch_changed() -> None:
    """A genuine epoch change keeps its own type, so the two stay distinguishable."""
    incoming = asyncio.StreamReader()
    incoming.feed_data(_golden("welcome.bin"))
    writer = _RecordingWriter()
    client = FrameClient(incoming, cast(Any, writer))
    locator = AttachLocator(backend="native", frame_host_epoch="epoch-old", host_terminal_id="ht-1")

    with pytest.raises(HostEpochChangedError) as failure:
        await client.handshake(locator, local_token="token")

    assert not isinstance(failure.value, HostNotAdoptedError)
    assert writer.writes, "the hello went out and the welcome epoch was compared"
    assert writer.closed is True


async def test_pump_marks_the_client_closed_at_eof() -> None:
    reader = asyncio.StreamReader()
    writer = _RecordingWriter()
    client = FrameClient(reader, cast(Any, writer))
    forgotten = asyncio.Event()

    client.start_pump(on_closed=forgotten.set)
    client.start_pump(on_closed=pytest.fail)
    reader.feed_data(_golden("frame.bin"))
    await asyncio.sleep(0)
    assert client.closed is False, "frames the daemon never renders are drained, not fatal"

    reader.feed_eof()
    await asyncio.wait_for(forgotten.wait(), timeout=2)

    assert client.closed is True
    assert writer.closed is True, "EOF from the host closes the daemon side too"


async def test_close_cancels_the_pump() -> None:
    reader = asyncio.StreamReader()
    writer = _RecordingWriter()
    client = FrameClient(reader, cast(Any, writer))
    client.start_pump()
    pump = client._pump_task
    assert pump is not None

    await client.close()
    await asyncio.sleep(0)

    assert pump.cancelled() or pump.done()
    assert client._pump_task is None
    client.start_pump()
    assert client._pump_task is None, "a closed client never starts a pump"


GOLDEN_THEME = {
    "foreground": {"r": 0x20, "g": 0x21, "b": 0x22},
    "background": {"r": 0xFA, "g": 0xFB, "b": 0xFC},
    "palette": [[1, {"r": 0xC0, "g": 0x10, "b": 0x20}]],
}


async def test_declare_terminal_theme_binds_then_declares_in_gterm_bytes() -> None:
    writer = _RecordingWriter()
    client = FrameClient(asyncio.StreamReader(), cast(Any, writer))

    await client.declare_terminal_theme("att-1", GOLDEN_THEME)

    assert writer.writes == [
        _golden("frame_bind_attachment.bin"),
        _golden("set_terminal_theme.bin"),
    ]


def test_a_theme_without_colours_encodes_empty_options() -> None:
    frame = encode_frame(
        {
            "type": "set_terminal_theme",
            "theme": {"foreground": None, "background": None, "palette": []},
        }
    )

    assert frame == b"\x04\x00\x00\x00\x0c\x00\x00\x00"


@pytest.mark.parametrize(
    "theme",
    [
        "dark",
        {"foreground": {"r": 256, "g": 0, "b": 0}},
        {"foreground": {"r": True, "g": 0, "b": 0}},
        {"background": {"r": 1, "g": 2}},
        {"palette": [[300, {"r": 0, "g": 0, "b": 0}]]},
        {"palette": [[1]]},
        {"palette": {"1": {"r": 0, "g": 0, "b": 0}}},
    ],
)
async def test_an_invalid_theme_is_refused_before_anything_is_written(theme: object) -> None:
    writer = _RecordingWriter()
    client = FrameClient(asyncio.StreamReader(), cast(Any, writer))

    with pytest.raises(FrameProtocolError):
        await client.declare_terminal_theme("att-1", theme)

    assert writer.writes == []
