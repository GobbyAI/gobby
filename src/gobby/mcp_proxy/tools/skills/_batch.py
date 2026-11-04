"""Pack several whole instruction bodies into lossless, budgeted pages.

A batch page carries whole bodies in request order. A body that does not fit
beside earlier bodies moves to the next page; a body too large for any page is
paged alone. Each body ends with a visible end marker, and a body is listed in
``page.completed`` only on the page that delivers its final byte.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from gobby.mcp_proxy.tools.skills._paging import (
    RESPONSE_BUDGET_BYTES,
    CursorError,
    content_hash,
    semantic_length,
    serialized_size,
)

BATCH_CURSOR_PREFIX = "b."
BATCH_CURSOR_VERSION = 1
MAX_BATCH_NAMES = 24
_MAX_CURSOR_LENGTH = 4096
_CURSOR_KEYS = {"names", "levels", "index", "offset", "content_hash", "version"}


@dataclass(frozen=True, slots=True)
class BatchCursor:
    """Identity needed to continue one batch of instruction bodies."""

    names: tuple[str, ...]
    levels: tuple[tuple[str, str], ...]
    index: int
    offset: int
    content_hash: str
    version: int = BATCH_CURSOR_VERSION


def end_marker(identity: str) -> str:
    return f"<!-- end: {identity} -->\n"


def marked_body(identity: str, content: str) -> str:
    """Return the body with its trailing end marker on its own line."""
    separator = "" if not content or content.endswith("\n") else "\n"
    return f"{content}{separator}{end_marker(identity)}"


def batch_hash(bodies: Sequence[tuple[str, str]]) -> str:
    return content_hash(json.dumps([list(body) for body in bodies], ensure_ascii=False))


def is_batch_cursor(value: str) -> bool:
    return value.startswith(BATCH_CURSOR_PREFIX)


def encode_batch_cursor(state: BatchCursor) -> str:
    payload = asdict(state)
    payload["names"] = list(state.names)
    payload["levels"] = dict(state.levels)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = BATCH_CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(encoded) > _MAX_CURSOR_LENGTH:
        raise ValueError("Batch is too large to continue; request fewer names")
    return encoded


def decode_batch_cursor(value: str) -> BatchCursor:
    if not is_batch_cursor(value) or len(value) > _MAX_CURSOR_LENGTH:
        raise CursorError("Cursor is malformed")
    body = value[len(BATCH_CURSOR_PREFIX) :]
    try:
        raw = base64.b64decode(body + "=" * (-len(body) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("Cursor is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != _CURSOR_KEYS:
        raise CursorError("Cursor has an invalid shape")
    names, levels = payload["names"], payload["levels"]
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) and name for name in names)
    ):
        raise CursorError("Cursor names are invalid")
    if not isinstance(levels, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in levels.items()
    ):
        raise CursorError("Cursor levels are invalid")
    for field in ("index", "offset", "version"):
        number = payload[field]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise CursorError(f"Cursor {field} is invalid")
    if payload["version"] != BATCH_CURSOR_VERSION:
        raise CursorError("Cursor version is unsupported")
    digest = payload["content_hash"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise CursorError("Cursor content hash is invalid")
    return BatchCursor(
        names=tuple(names),
        levels=tuple(sorted(levels.items())),
        index=payload["index"],
        offset=payload["offset"],
        content_hash=digest,
    )


def build_batch_page(
    bodies: Sequence[tuple[str, str]],
    state: BatchCursor,
    unavailable: list[dict[str, str]],
) -> dict[str, Any]:
    """Build the next page of ``bodies`` (identity, marked content) from ``state``."""
    total = len(bodies)
    if state.index > total or (state.index == total and state.offset):
        raise CursorError("Cursor position exceeds the batch")

    def continue_at(index: int, offset: int) -> str | None:
        if index >= total:
            return None
        return encode_batch_cursor(replace(state, index=index, offset=offset))

    def response(
        items: list[tuple[str, str]], completed: list[str], next_cursor: str | None
    ) -> dict[str, Any]:
        page: dict[str, Any] = {
            "success": True,
            "view": "brief",
            "instructions": [{"identity": identity, "content": text} for identity, text in items],
            "page": {
                "completed": completed,
                "complete": next_cursor is None,
                "next_cursor": next_cursor,
            },
        }
        if unavailable:
            page["unavailable"] = unavailable
        return page

    index = state.index
    if state.offset == 0:
        items: list[tuple[str, str]] = []
        while index < total:
            trial = [*items, bodies[index]]
            completed = [identity for identity, _ in trial]
            if serialized_size(response(trial, completed, continue_at(index + 1, 0))) > (
                RESPONSE_BUDGET_BYTES
            ):
                break
            items = trial
            index += 1
        if items or index == total:
            return response(items, [identity for identity, _ in items], continue_at(index, 0))
    return _oversized_body_page(bodies, state, index, continue_at, response)


def _oversized_body_page(
    bodies: Sequence[tuple[str, str]],
    state: BatchCursor,
    index: int,
    continue_at: Callable[[int, int], str | None],
    response: Callable[[list[tuple[str, str]], list[str], str | None], dict[str, Any]],
) -> dict[str, Any]:
    """Page one body that no page can hold whole, starting at the cursor offset."""
    identity, content = bodies[index]
    offset = state.offset
    encoded = content.encode("utf-8")
    if offset > len(encoded):
        raise CursorError("Cursor offset exceeds content length")
    try:
        remaining = encoded[offset:].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CursorError("Cursor offset is not a UTF-8 boundary") from exc

    def candidate(character_count: int) -> dict[str, Any]:
        chunk = remaining[:character_count]
        complete = character_count == len(remaining)
        end = offset + len(chunk.encode("utf-8"))
        next_cursor = continue_at(index + 1, 0) if complete else continue_at(index, end)
        return response([(identity, chunk)], [identity] if complete else [], next_cursor)

    low, high = 0, len(remaining)
    while low < high:
        middle = (low + high + 1) // 2
        if serialized_size(candidate(middle)) <= RESPONSE_BUDGET_BYTES:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        raise ValueError("Batch response metadata leaves no room for content")
    count = semantic_length(remaining[:low]) if low < len(remaining) else low
    return candidate(count)
