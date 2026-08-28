"""Opaque cursor and lossless UTF-8 paging helpers for skill delivery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

RESPONSE_BUDGET_BYTES = 15_000
CURSOR_VERSION = 1

PageKind = Literal["skill", "file"]
ResponseView = Literal["brief", "full"]


class CursorError(ValueError):
    """A malformed or tool-inconsistent skill delivery cursor."""


@dataclass(frozen=True, slots=True)
class CursorState:
    """Identity needed to continue one immutable content stream."""

    kind: PageKind
    view: ResponseView
    skill_id: str
    path: str | None
    level: str | None
    content_hash: str
    offset: int
    version: int = CURSOR_VERSION


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def encode_cursor(state: CursorState) -> str:
    payload = json.dumps(asdict(state), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(value: str, *, expected_kind: PageKind) -> CursorState:
    if not value or len(value) > 4096:
        raise CursorError("Cursor is malformed")
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorError("Cursor is malformed") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "kind",
        "view",
        "skill_id",
        "path",
        "level",
        "content_hash",
        "offset",
        "version",
    }:
        raise CursorError("Cursor has an invalid shape")
    try:
        state = CursorState(**payload)
    except TypeError as exc:
        raise CursorError("Cursor has invalid fields") from exc
    if state.version != CURSOR_VERSION:
        raise CursorError("Cursor version is unsupported")
    if state.kind not in {"skill", "file"} or state.kind != expected_kind:
        raise CursorError("Cursor belongs to a different tool")
    if state.view not in {"brief", "full"}:
        raise CursorError("Cursor view is invalid")
    if not isinstance(state.skill_id, str) or not state.skill_id:
        raise CursorError("Cursor skill identity is invalid")
    if state.kind == "skill" and state.path is not None:
        raise CursorError("Skill cursor cannot contain a file path")
    if state.kind == "file" and (not isinstance(state.path, str) or not state.path):
        raise CursorError("File cursor requires a path")
    if state.level is not None and not isinstance(state.level, str):
        raise CursorError("Cursor level is invalid")
    if (
        not isinstance(state.content_hash, str)
        or len(state.content_hash) != 64
        or any(character not in "0123456789abcdef" for character in state.content_hash)
    ):
        raise CursorError("Cursor content hash is invalid")
    if isinstance(state.offset, bool) or not isinstance(state.offset, int) or state.offset < 0:
        raise CursorError("Cursor offset is invalid")
    return state


def serialized_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _semantic_length(value: str) -> int:
    blank_line = value.rfind("\n\n")
    if blank_line >= 0:
        return blank_line + 2
    newline = value.rfind("\n")
    if newline >= 0:
        return newline + 1
    return len(value)


def build_content_page(
    content: str,
    state: CursorState,
    response_factory: Callable[[str, int, int, bool, str | None], dict[str, Any]],
) -> dict[str, Any]:
    """Build the largest semantic, lossless page inside the serialized response budget."""
    encoded = content.encode("utf-8")
    if state.offset > len(encoded):
        raise CursorError("Cursor offset exceeds content length")
    try:
        remaining = encoded[state.offset :].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CursorError("Cursor offset is not a UTF-8 boundary") from exc

    def candidate(character_count: int) -> dict[str, Any]:
        chunk = remaining[:character_count]
        end = state.offset + len(chunk.encode("utf-8"))
        complete = character_count == len(remaining)
        next_cursor = None if complete else encode_cursor(replace(state, offset=end))
        return response_factory(chunk, state.offset, end, complete, next_cursor)

    low = 0
    high = len(remaining)
    while low < high:
        middle = (low + high + 1) // 2
        if serialized_size(candidate(middle)) <= RESPONSE_BUDGET_BYTES:
            low = middle
        else:
            high = middle - 1
    if low == 0 and remaining:
        raise ValueError("Skill response metadata leaves no room for content")

    semantic_count = _semantic_length(remaining[:low]) if low < len(remaining) else low
    response = candidate(semantic_count)
    if serialized_size(response) > RESPONSE_BUDGET_BYTES:
        raise ValueError("Skill response exceeds the internal delivery budget")
    return response
