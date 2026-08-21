"""Async bincode frame client for gterm-frames.sock (plan 4.1.8)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from gobby.storage.terminals import AttachLocator
from gobby.terminals.dimensions import MAX_FRAME_SIZE
from gobby.terminals.host_client import HostEpochChangedError

PROTOCOL_VERSION = 1
DELTA_QUEUE_ENTRIES = 64
DELTA_QUEUE_BYTES = MAX_FRAME_SIZE


class FrameProtocolError(RuntimeError):
    """Length, version, or bincode decode failure on the frame socket."""


class FrameLagError(RuntimeError):
    """Bounded frame queue overflow; the attachment must close."""


class FrameSocketWriter(Protocol):
    """Writable half of a Unix-domain frame connection."""

    def write(self, data: bytes) -> object: ...

    async def drain(self) -> object: ...

    def close(self) -> object: ...

    async def wait_closed(self) -> object: ...


_SINGLE_BYTE_MAX = 250
_U16_BYTE = 251
_U32_BYTE = 252
_U64_BYTE = 253


def _uvarint(value: int) -> bytes:
    """Bincode 2 `standard()` unsigned varint (not protobuf uleb128)."""
    if value < 0:
        raise FrameProtocolError("negative varint")
    if value <= _SINGLE_BYTE_MAX:
        return bytes((value,))
    if value <= 0xFFFF:
        return bytes((_U16_BYTE,)) + value.to_bytes(2, "little")
    if value <= 0xFFFFFFFF:
        return bytes((_U32_BYTE,)) + value.to_bytes(4, "little")
    if value <= 0xFFFFFFFFFFFFFFFF:
        return bytes((_U64_BYTE,)) + value.to_bytes(8, "little")
    raise FrameProtocolError("varint overflow")


def _ivarint(value: int) -> bytes:
    zigzag = (value << 1) ^ (value >> 63)
    return _uvarint(zigzag & ((1 << 64) - 1) if zigzag < 0 else zigzag)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _uvarint(len(encoded)) + encoded


def _bytes(value: bytes) -> bytes:
    return _uvarint(len(value)) + value


def _bool(value: bool) -> bytes:
    return b"\x01" if value else b"\x00"


def _option(value: object | None, encode_item: Callable[[Any], bytes]) -> bytes:
    if value is None:
        return b"\x00"
    return b"\x01" + encode_item(value)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def remaining(self) -> int:
        return len(self.data) - self.offset

    def take(self, n: int) -> bytes:
        if self.remaining() < n:
            raise FrameProtocolError("unexpected eof")
        chunk = self.data[self.offset : self.offset + n]
        self.offset += n
        return chunk

    def uvarint(self) -> int:
        if self.remaining() <= 0:
            raise FrameProtocolError("unexpected eof")
        byte = self.data[self.offset]
        self.offset += 1
        if byte <= _SINGLE_BYTE_MAX:
            return byte
        if byte == _U16_BYTE:
            return int.from_bytes(self.take(2), "little")
        if byte == _U32_BYTE:
            return int.from_bytes(self.take(4), "little")
        if byte == _U64_BYTE:
            return int.from_bytes(self.take(8), "little")
        raise FrameProtocolError("varint overflow")

    def ivarint(self) -> int:
        raw = self.uvarint()
        return (raw >> 1) ^ -(raw & 1)

    def string(self) -> str:
        length = self.uvarint()
        return self.take(length).decode("utf-8")

    def blob(self) -> bytes:
        length = self.uvarint()
        return self.take(length)

    def boolean(self) -> bool:
        return self.take(1) != b"\x00"


def _encode_identity(identity: dict[str, Any]) -> bytes:
    return (
        _string(str(identity["socket_path"]))
        + _ivarint(int(identity["server_pid"]))
        + _ivarint(int(identity["server_start_time"]))
        + _string(str(identity["pane_id"]))
    )


def _encode_hello(payload: dict[str, Any]) -> bytes:
    encoding = payload.get("encoding", "semantic_frame")
    encoding_tag = 0 if encoding in {"semantic_frame", "SemanticFrame", 0} else 1
    body = (
        b"\x00"
        + _uvarint(int(payload.get("version", PROTOCOL_VERSION)))
        + _uvarint(encoding_tag)
        + _string(str(payload["local_token"]))
        + _uvarint(int(payload["cols"]))
        + _uvarint(int(payload["rows"]))
        + _option(payload.get("tmux_identity"), _encode_identity)
    )
    if int(payload.get("version", PROTOCOL_VERSION)) != PROTOCOL_VERSION:
        raise FrameProtocolError("unsupported protocol version")
    return body


def _encode_attach(payload: dict[str, Any]) -> bytes:
    return (
        b"\x05"
        + _string(str(payload["host_terminal_id"]))
        + _option(payload.get("reservation_id"), _string)
        + _option(payload.get("locator"), _encode_identity)
    )


def _encode_payload(payload: dict[str, Any]) -> bytes:
    kind = payload["type"]
    if kind == "hello":
        return _encode_hello(payload)
    if kind == "detach":
        return b"\x04"
    if kind == "attach_terminal":
        return _encode_attach(payload)
    if kind == "set_viewport":
        return b"\x06" + _uvarint(int(payload["rows"])) + _uvarint(int(payload["cols"]))
    if kind == "set_scroll_offset":
        return b"\x07" + _uvarint(int(payload["rows_from_live_edge"]))
    raise FrameProtocolError(f"cannot encode {kind}")


def encode_frame(payload: dict[str, Any]) -> bytes:
    body = _encode_payload(payload)
    return len(body).to_bytes(4, "little") + body


def _decode_modes(reader: _Reader) -> dict[str, Any]:
    return {
        "cursor_visible": reader.boolean(),
        "cursor_very_visible": reader.boolean(),
        "cursor_shape": reader.uvarint(),
        "cursor_blinking": reader.boolean(),
        "cursor_colour": reader.string(),
        "alternate_on": reader.boolean(),
        "keypad_cursor": reader.boolean(),
        "keypad": reader.boolean(),
        "bracket_paste": reader.boolean(),
        "mouse_standard": reader.boolean(),
        "mouse_button": reader.boolean(),
        "mouse_any": reader.boolean(),
        "mouse_all": reader.boolean(),
        "mouse_sgr": reader.boolean(),
        "mouse_utf8": reader.boolean(),
        "wrap": reader.boolean(),
        "origin": reader.boolean(),
        "insert": reader.boolean(),
        "scroll_region_upper": reader.uvarint(),
        "scroll_region_lower": reader.uvarint(),
        "pane_in_mode": reader.boolean(),
    }


def _decode_cursor(reader: _Reader) -> dict[str, Any] | None:
    tag = reader.uvarint()
    if tag == 0:
        return None
    return {
        "x": reader.uvarint(),
        "y": reader.uvarint(),
        "visible": reader.boolean(),
        "shape": reader.uvarint(),
    }


def _decode_server(reader: _Reader) -> dict[str, Any]:
    tag = reader.uvarint()
    if tag == 0:
        return {"type": "welcome", "host_epoch": reader.string()}
    if tag == 1:
        frame = _decode_frame_data_correct(reader)
        return {"type": "frame", **frame}
    if tag == 2:
        return {
            "type": "terminal",
            "seq": reader.uvarint(),
            "width": reader.uvarint(),
            "height": reader.uvarint(),
            "full": reader.boolean(),
            "bytes": reader.blob(),
        }
    if tag == 3:
        return {"type": "graphics", "bytes": reader.blob()}
    if tag == 4:
        return {
            "type": "attach_history",
            "text": reader.string(),
            "truncated": reader.boolean(),
            "dropped_bytes": reader.uvarint(),
            "total_bytes": reader.uvarint(),
        }
    if tag == 5:
        return {
            "type": "scroll_offset_applied",
            "applied_rows": reader.uvarint(),
            "max_rows": reader.uvarint(),
        }
    if tag == 6:
        host_terminal_id = reader.string()
        exit_tag = reader.uvarint()
        exit_code = None if exit_tag == 0 else reader.ivarint()
        return {
            "type": "terminal_exited",
            "host_terminal_id": host_terminal_id,
            "exit_code": exit_code,
        }
    if tag == 7:
        return {
            "type": "error",
            "code": reader.string(),
            "message": None if reader.uvarint() == 0 else reader.string(),
        }
    if tag == 8:
        return {
            "type": "attached",
            "created": reader.boolean(),
            "host_terminal_id": reader.string(),
        }
    raise FrameProtocolError(f"unknown server tag {tag}")


def _decode_frame_data_correct(reader: _Reader) -> dict[str, Any]:
    count = reader.uvarint()
    cells = []
    for _ in range(count):
        symbol = reader.string()
        fg = reader.uvarint()
        bg = reader.uvarint()
        modifier = reader.uvarint()
        skip = reader.boolean()
        link_tag = reader.uvarint()
        hyperlink = None if link_tag == 0 else reader.uvarint()
        cells.append(
            {
                "symbol": symbol,
                "fg": fg,
                "bg": bg,
                "modifier": modifier,
                "skip": skip,
                "hyperlink": hyperlink,
            }
        )
    width = reader.uvarint()
    height = reader.uvarint()
    cursor = _decode_cursor(reader)
    n_links = reader.uvarint()
    hyperlinks = [reader.string() for _ in range(n_links)]
    graphics = reader.blob()
    modes = _decode_modes(reader)
    return {
        "cells": cells,
        "width": width,
        "height": height,
        "cursor": cursor,
        "hyperlinks": hyperlinks,
        "graphics": graphics,
        "modes": modes,
    }


def _decode_client(reader: _Reader) -> dict[str, Any]:
    tag = reader.uvarint()
    if tag == 0:
        version = reader.uvarint()
        if version != PROTOCOL_VERSION:
            raise FrameProtocolError("unsupported protocol version")
        encoding = "semantic_frame" if reader.uvarint() == 0 else "terminal_ansi"
        token = reader.string()
        cols = reader.uvarint()
        rows = reader.uvarint()
        ident_tag = reader.uvarint()
        identity = None
        if ident_tag == 1:
            identity = {
                "socket_path": reader.string(),
                "server_pid": reader.ivarint(),
                "server_start_time": reader.ivarint(),
                "pane_id": reader.string(),
            }
        return {
            "type": "hello",
            "version": version,
            "encoding": encoding,
            "local_token": token,
            "cols": cols,
            "rows": rows,
            "tmux_identity": identity,
        }
    if tag == 4:
        return {"type": "detach"}
    if tag == 5:
        host_terminal_id = reader.string()
        res_tag = reader.uvarint()
        reservation_id = None if res_tag == 0 else reader.string()
        loc_tag = reader.uvarint()
        locator = None
        if loc_tag == 1:
            locator = {
                "socket_path": reader.string(),
                "server_pid": reader.ivarint(),
                "server_start_time": reader.ivarint(),
                "pane_id": reader.string(),
            }
        return {
            "type": "attach_terminal",
            "host_terminal_id": host_terminal_id,
            "reservation_id": reservation_id,
            "locator": locator,
        }
    if tag == 6:
        return {"type": "set_viewport", "rows": reader.uvarint(), "cols": reader.uvarint()}
    if tag == 7:
        return {"type": "set_scroll_offset", "rows_from_live_edge": reader.uvarint()}
    raise FrameProtocolError(f"unknown client tag {tag}")


def decode_frame(raw: bytes) -> dict[str, Any]:
    if len(raw) < 4:
        raise FrameProtocolError("short frame")
    claimed = int.from_bytes(raw[:4], "little")
    if claimed > MAX_FRAME_SIZE:
        raise FrameProtocolError("oversized frame")
    payload = raw[4:]
    if len(payload) < claimed:
        # allow decode of a complete in-memory frame only
        if len(payload) != claimed and len(raw) >= 4:
            # still try if payload is exactly the rest
            payload = raw[4 : 4 + claimed] if len(raw) >= 4 + claimed else payload
        if len(payload) < claimed:
            raise FrameProtocolError("truncated frame")
        payload = payload[:claimed]
    else:
        payload = payload[:claimed]
    tag = payload[0] if payload else 255
    if tag == 0 and claimed > 13 and len(payload) > 1 and payload[1] == PROTOCOL_VERSION:
        return _decode_client(_Reader(payload))
    if tag == 4 and claimed == 1:
        return {"type": "detach"}
    if tag == 5 and claimed > 8:
        return _decode_client(_Reader(payload))
    if tag == 6 and claimed == 3:
        return _decode_client(_Reader(payload))
    if tag == 7 and claimed == 2:
        return _decode_client(_Reader(payload))
    return _decode_server(_Reader(payload))


class FrameClient:
    """Reader-only length-prefixed bincode client."""

    def __init__(self, reader: asyncio.StreamReader, writer: FrameSocketWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.attached = False
        self.closed = False
        self._queue: list[dict[str, Any]] = []
        self._queue_bytes = 0

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.attached = False
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (OSError, ConnectionError):
            return

    async def read_message(self) -> dict[str, Any]:
        header = await self._read_exact(4)
        claimed = int.from_bytes(header, "little")
        if claimed > MAX_FRAME_SIZE:
            raise FrameProtocolError("oversized frame")
        payload = await self._read_exact(claimed)
        return decode_frame(header + payload)

    async def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = await self._reader.read(n - len(buf))
            if not chunk:
                raise FrameProtocolError("unexpected eof")
            buf.extend(chunk)
        return bytes(buf)

    async def _send(self, payload: dict[str, Any]) -> None:
        self._writer.write(encode_frame(payload))
        await self._writer.drain()

    async def handshake(
        self,
        locator: AttachLocator,
        *,
        local_token: str | None = None,
        encoding: str = "semantic_frame",
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        token = local_token if local_token is not None else _read_local_cli_token()
        await self._send(
            {
                "type": "hello",
                "version": PROTOCOL_VERSION,
                "encoding": encoding,
                "local_token": token,
                "cols": cols,
                "rows": rows,
                "tmux_identity": None,
            }
        )
        welcome = await self.read_message()
        if welcome.get("type") != "welcome":
            raise FrameProtocolError(f"expected welcome, got {welcome.get('type')}")
        if str(welcome.get("host_epoch")) != locator.frame_host_epoch:
            await self.close()
            raise HostEpochChangedError("host epoch changed")

    async def attach_terminal(
        self,
        locator: AttachLocator,
        *,
        reservation_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "type": "attach_terminal",
            "host_terminal_id": locator.host_terminal_id or "",
            "reservation_id": reservation_id,
            "locator": None,
        }
        if locator.backend == "tmux" and locator.socket_path and locator.pane_id:
            pid = locator.server_pid
            start = locator.server_start_time
            if not isinstance(pid, int) or not isinstance(start, int):
                raise FrameProtocolError("tmux attach requires server generation")
            payload["locator"] = {
                "socket_path": locator.socket_path,
                "server_pid": pid,
                "server_start_time": start,
                "pane_id": locator.pane_id,
            }
        await self._send(payload)
        self.attached = True

    async def set_viewport(self, rows: int, cols: int) -> None:
        await self._send({"type": "set_viewport", "rows": rows, "cols": cols})

    async def set_scroll_offset(self, rows_from_live_edge: int) -> None:
        await self._send({"type": "set_scroll_offset", "rows_from_live_edge": rows_from_live_edge})

    async def detach(self) -> None:
        await self._send({"type": "detach"})
        self.attached = False

    def enqueue(self, message: dict[str, Any]) -> None:
        encoded = repr(message).encode("utf-8")
        if (
            len(self._queue) >= DELTA_QUEUE_ENTRIES
            or self._queue_bytes + len(encoded) > DELTA_QUEUE_BYTES
        ):
            raise FrameLagError("frame queue overflow")
        self._queue.append(message)
        self._queue_bytes += len(encoded)


def _read_local_cli_token() -> str:
    path = Path.home() / ".gobby" / "local_cli_token"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


__all__ = [
    "DELTA_QUEUE_ENTRIES",
    "FrameClient",
    "FrameLagError",
    "FrameProtocolError",
    "decode_frame",
    "encode_frame",
]
