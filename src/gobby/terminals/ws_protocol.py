"""Backend-neutral terminal WebSocket protocol constants and codecs."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Buffer, Callable, Iterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

TERMINAL_WS_SAFE_INTEGER_MAX: Final[int] = 2**53 - 1
TERMINAL_LIST_DEFAULT_PAGE_SIZE: Final[int] = 100
TERMINAL_LIST_MAX_PAGE_SIZE: Final[int] = 500
TERMINAL_LIST_MAX_ENCODED_BYTES: Final[int] = 1024 * 1024
TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES: Final[int] = 16 * 1024 * 1024
TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES: Final[int] = 64 * 1024 * 1024
TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES: Final[int] = 2 * 1024 * 1024
TERMINAL_WS_FRAGMENT_REASSEMBLY_TIMEOUT_MS: Final[int] = 5000
TERMINAL_WS_FRAME_QUEUE_ENTRIES: Final[int] = 64
TERMINAL_WS_FRAME_QUEUE_BYTES: Final[int] = 2 * 1024 * 1024
TERMINAL_WS_FRAME_SEND_TIMEOUT_S: Final[float] = 5.0
TERMINAL_WS_LIFECYCLE_RESERVE_MAX_ENTRIES: Final[int] = 16
TERMINAL_WS_LIFECYCLE_RESERVE_MAX_BYTES: Final[int] = 64 * 1024
TERMINAL_WS_LIFECYCLE_SEND_TIMEOUT_S: Final[float] = 2.0
PASTE_MAX_BYTES: Final[int] = 1024 * 1024
WRITE_SEQ_CAPACITY: Final[int] = 64

SAFE_INTEGER_FIELDS: Final[frozenset[str]] = frozenset(
    {"message_seq", "lease_generation", "client_write_seq", "fragment_index", "seq"}
)


class SafeIntegerOverflowError(ValueError):
    """Raised when a protocol counter is outside JavaScript's safe integer range."""

    def __init__(self) -> None:
        super().__init__("safe_integer_overflow")


class TerminalPageTooLargeError(ValueError):
    """No terminal inventory row fits in the encoded page budget."""

    def __init__(self) -> None:
        super().__init__("terminal_page_too_large")


def _check_safe_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SafeIntegerOverflowError()
    if value < 0 or value > TERMINAL_WS_SAFE_INTEGER_MAX:
        raise SafeIntegerOverflowError()
    return value


def _walk_safe_ints(payload: object) -> None:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if key in SAFE_INTEGER_FIELDS:
                _check_safe_int(value, str(key))
            else:
                _walk_safe_ints(value)
    elif isinstance(payload, list):
        for item in payload:
            _walk_safe_ints(item)


def encode_message(message: Mapping[str, Any]) -> bytes:
    """Serialize a protocol object to canonical JSON bytes."""
    if "mode" in message:
        raise ValueError("terminal WS messages must not carry mode")
    _walk_safe_ints(message)
    dumped = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return (dumped + "\n").encode("utf-8")


def decode_message(raw: bytes | str) -> dict[str, Any]:
    """Parse a canonical protocol payload."""
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict):
        raise TypeError("terminal WS payload must be an object")
    _walk_safe_ints(parsed)
    return parsed


def canonical_json(message: Mapping[str, Any]) -> bytes:
    """Serialize without the trailing newline used by encode_message."""
    if "mode" in message:
        raise ValueError("terminal WS messages must not carry mode")
    _walk_safe_ints(message)
    return json.dumps(
        dict(message), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


# A long string is encoded in pieces of at most this many characters. The async encoder
# pauses for the event loop after about this much output.
CANONICAL_PIECE_CHARS: Final[int] = 64 * 1024
_CANONICAL_ENCODER = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _canonical_pieces(value: Any) -> Iterator[str]:
    """Yield text pieces whose concatenation is canonical_json's text for ``value``.

    JSON escapes each character on its own, so a slice of a string encodes to the same
    text as the whole string. Keys are sorted as json sorts them, and the safe-integer
    fields are checked as _walk_safe_ints checks them.
    """
    if isinstance(value, str):
        if len(value) <= CANONICAL_PIECE_CHARS:
            yield _CANONICAL_ENCODER.encode(value)
            return
        yield '"'
        for start in range(0, len(value), CANONICAL_PIECE_CHARS):
            yield _CANONICAL_ENCODER.encode(value[start : start + CANONICAL_PIECE_CHARS])[1:-1]
        yield '"'
    elif isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            # json converts or rejects non-string keys itself, so the whole dict goes to it.
            _walk_safe_ints(value)
            yield _CANONICAL_ENCODER.encode(value)
            return
        yield "{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield ","
            yield _CANONICAL_ENCODER.encode(key)
            yield ":"
            if key in SAFE_INTEGER_FIELDS:
                _check_safe_int(value[key], key)
            yield from _canonical_pieces(value[key])
        yield "}"
    elif isinstance(value, (list, tuple)):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _canonical_pieces(item)
        yield "]"
    elif value is None or isinstance(value, bool):
        yield {None: "null", True: "true", False: "false"}[value]
    elif type(value) is int:
        yield int.__repr__(value)
    else:
        yield _CANONICAL_ENCODER.encode(value)


async def _pause_for_loop() -> None:
    await asyncio.sleep(0)


async def canonical_json_async(
    message: Mapping[str, Any],
    *,
    pause: Callable[[], Awaitable[None]] = _pause_for_loop,
) -> bytearray:
    """Encode like canonical_json, pausing for the event loop between pieces of the text.

    The bytes equal canonical_json's. No single step holds the loop for more than about
    CANONICAL_PIECE_CHARS characters of output, so a large event does not stall other tasks.
    The buffer is returned as built, without a copy; it is never written to after this call.
    """
    if "mode" in message:
        raise ValueError("terminal WS messages must not carry mode")
    encoded = bytearray()
    pending = 0
    for piece in _canonical_pieces(dict(message)):
        encoded += piece.encode("utf-8")
        pending += len(piece)
        if pending >= CANONICAL_PIECE_CHARS:
            pending = 0
            await pause()
    return encoded


def _fragment_dict(
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
    index: int,
    more: bool,
    payload: str,
) -> dict[str, Any]:
    return {
        "type": "terminal_ws_fragment",
        "event": event,
        "terminal_id": terminal_id,
        "attachment_id": attachment_id,
        "message_seq": message_seq,
        "fragment_index": index,
        "more": more,
        "encoding": "utf8-b64",
        "payload": payload,
    }


# Base64 is built from input blocks of this size. It is a multiple of 3, so each block encodes
# to whole base64 groups and the blocks' text concatenates to the whole payload's base64.
PAYLOAD_BLOCK_BYTES: Final[int] = 48 * 1024


def _payload_blocks(chunk: Buffer) -> Iterator[str]:
    """Yield the base64 text of ``chunk`` one block at a time."""
    view = memoryview(chunk)
    for start in range(0, len(view), PAYLOAD_BLOCK_BYTES):
        yield base64.b64encode(view[start : start + PAYLOAD_BLOCK_BYTES]).decode("ascii")


def _wrapped_size(
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
    index: int,
    more: bool,
    size: int,
) -> int:
    """Return the wrapped length of a fragment with a `size`-byte slice, without encoding it.

    The base64 alphabet needs no JSON escaping, so the payload adds 4 * ceil(size / 3) bytes.
    """
    envelope = _fragment_dict(
        event=event,
        terminal_id=terminal_id,
        attachment_id=attachment_id,
        message_seq=message_seq,
        index=index,
        more=more,
        payload="",
    )
    return len(canonical_json(envelope)) + (size + 2) // 3 * 4


def _greedy_chunks(
    complete_json: memoryview,
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
) -> list[memoryview]:
    chunks: list[memoryview] = []
    offset = 0
    # A wrapped fragment holds its base64 payload, so a take above this bound never fits.
    # Halving to the bound first skips only probes that cannot fit.
    bound = TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES // 4 * 3
    while offset < len(complete_json):
        remaining = len(complete_json) - offset
        take = remaining
        while take > bound:
            take //= 2
        while take > 0:
            size = _wrapped_size(
                event=event,
                terminal_id=terminal_id,
                attachment_id=attachment_id,
                message_seq=message_seq,
                index=len(chunks),
                more=offset + take < len(complete_json),
                size=take,
            )
            if size < TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES:
                chunks.append(complete_json[offset : offset + take])
                offset += take
                break
            take = max(1, take // 2)
            if take == 1 and size >= TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES:
                raise ValueError("fragment_too_large")
        else:
            raise ValueError("fragment_too_large")
    return chunks


def _fragment_plan(
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
    complete_json: Buffer,
) -> list[tuple[int, memoryview, bool]]:
    """Return (index, slice, more) for each fragment of a complete JSON payload.

    The slices are views of the payload, and sizes come from _wrapped_size, so planning
    encodes no payload.
    """
    _check_safe_int(message_seq, "message_seq")
    # Slices of a view copy none of the payload, so cutting a 16 MiB event costs no copy.
    data = memoryview(complete_json)
    if len(data) > TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES:
        raise ValueError("fragment_too_large")
    midpoint = max(1, len(data) // 2)
    slices = [data[:midpoint], data[midpoint:]]
    if any(
        _wrapped_size(
            event=event,
            terminal_id=terminal_id,
            attachment_id=attachment_id,
            message_seq=message_seq,
            index=index,
            more=index < len(slices) - 1,
            size=len(chunk),
        )
        >= TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES
        for index, chunk in enumerate(slices)
    ):
        slices = _greedy_chunks(
            data,
            event=event,
            terminal_id=terminal_id,
            attachment_id=attachment_id,
            message_seq=message_seq,
        )
    return [
        (index, chunk, index < len(slices) - 1)
        for index, chunk in enumerate(slices)
        if chunk or index == 0
    ]


def iter_fragment_event(
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
    complete_json: Buffer,
) -> Iterator[dict[str, Any]]:
    """Yield the terminal_ws_fragment slices of a complete JSON payload, one at a time."""
    for index, chunk, more in _fragment_plan(
        event=event,
        terminal_id=terminal_id,
        attachment_id=attachment_id,
        message_seq=message_seq,
        complete_json=complete_json,
    ):
        yield _fragment_dict(
            event=event,
            terminal_id=terminal_id,
            attachment_id=attachment_id,
            message_seq=message_seq,
            index=index,
            more=more,
            payload="".join(_payload_blocks(chunk)),
        )


async def fragment_event_async(
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
    complete_json: Buffer,
) -> list[dict[str, Any]]:
    """Return fragment_event's frames, pausing for the event loop between base64 blocks.

    Each block encodes PAYLOAD_BLOCK_BYTES of input, so no single step holds the loop for
    longer than one block's encode.
    """
    fragments: list[dict[str, Any]] = []
    for index, chunk, more in _fragment_plan(
        event=event,
        terminal_id=terminal_id,
        attachment_id=attachment_id,
        message_seq=message_seq,
        complete_json=complete_json,
    ):
        blocks: list[str] = []
        for block in _payload_blocks(chunk):
            blocks.append(block)
            await _pause_for_loop()
        fragments.append(
            _fragment_dict(
                event=event,
                terminal_id=terminal_id,
                attachment_id=attachment_id,
                message_seq=message_seq,
                index=index,
                more=more,
                payload="".join(blocks),
            )
        )
    return fragments


def fragment_event(
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
    complete_json: bytes,
) -> list[dict[str, Any]]:
    """Split a complete JSON payload into terminal_ws_fragment slices."""
    return list(
        iter_fragment_event(
            event=event,
            terminal_id=terminal_id,
            attachment_id=attachment_id,
            message_seq=message_seq,
            complete_json=complete_json,
        )
    )


def emit_proxied_event(
    event: Mapping[str, Any],
    *,
    message_seq: int,
) -> list[tuple[dict[str, Any], bytes]]:
    """Return each wire payload with its canonical bytes, fragmenting above 2 MiB."""
    payload = dict(event)
    raw = canonical_json(payload)
    if len(raw) > TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES:
        raise ValueError("fragment_too_large")
    if len(raw) < TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES:
        return [(payload, raw)]
    fragments = fragment_event(
        event=str(payload["type"]),
        terminal_id=str(payload["terminal_id"]),
        attachment_id=str(payload["attachment_id"]),
        message_seq=message_seq,
        complete_json=raw,
    )
    return [(fragment, canonical_json(fragment)) for fragment in fragments]


def parse_list_cursor(raw: object) -> tuple[datetime | None, str | None]:
    """Split a ``<created_at iso>|<terminal_id>`` page cursor; raises ValueError when malformed."""
    if not raw:
        return None, None
    if not isinstance(raw, str):
        raise ValueError("cursor must be a string")
    created_at, _, terminal_id = raw.partition("|")
    if not terminal_id:
        raise ValueError("cursor is missing the terminal id")
    return datetime.fromisoformat(created_at), str(UUID(terminal_id))


def inventory_item(
    row: Any, *, lease_holder: dict[str, str | None] | None = None
) -> dict[str, Any]:
    """Backend-neutral list row."""
    dims = None
    if row.rows is not None and row.cols is not None:
        dims = {"rows": row.rows, "cols": row.cols}
    return {
        "terminal_id": row.id,
        "backend": row.backend,
        "ownership": row.ownership,
        "state": row.state,
        "title": row.title,
        "session_id": row.session_id,
        "agent_run_id": row.agent_run_id,
        "lease_holder": lease_holder,
        "dims": dims,
        "updated_at": row.updated_at.isoformat(),
    }


def encode_page(
    items: Sequence[Mapping[str, Any]],
    next_cursor: str | None,
    *,
    snapshot: Mapping[str, Any] | None = None,
    item_cursors: Sequence[str] | None = None,
    envelope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a byte-bounded inventory page with canonical truncation cursors."""

    def payload(count: int, cursor: str | None) -> dict[str, Any]:
        return {
            **dict(envelope or {}),
            "items": list(items[:count]),
            "next_cursor": cursor,
            "snapshot": None if snapshot is None else dict(snapshot),
        }

    def encoded_size(value: object) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    if not items:
        empty = payload(0, next_cursor)
        if encoded_size(empty) > TERMINAL_LIST_MAX_ENCODED_BYTES:
            raise TerminalPageTooLargeError()
        return empty

    # Re-encoding every candidate page was quadratic in the row count (#23359), so
    # each row is encoded once: a candidate is the empty page plus its rows, their
    # separating commas, and its cursor in place of the empty page's null.
    empty_size = encoded_size(payload(0, None)) - encoded_size(None)
    rows_size = 0
    selected_count = 0
    selected_cursor: str | None = None
    cursor: str | None
    for count in range(1, len(items) + 1):
        more_input = count < len(items)
        if more_input:
            if item_cursors is None or len(item_cursors) < count:
                raise ValueError("canonical item cursor required for page truncation")
            cursor = item_cursors[count - 1]
        else:
            cursor = next_cursor
        rows_size += encoded_size(items[count - 1]) + (1 if count > 1 else 0)
        if empty_size + rows_size + encoded_size(cursor) > TERMINAL_LIST_MAX_ENCODED_BYTES:
            break
        selected_count, selected_cursor = count, cursor
    if selected_count == 0:
        raise TerminalPageTooLargeError()
    return payload(selected_count, selected_cursor)
