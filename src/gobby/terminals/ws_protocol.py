"""Backend-neutral terminal WebSocket protocol constants and codecs."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
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
    {"message_seq", "lease_generation", "client_write_seq", "fragment_index"}
)

GOLDEN_NAMES: Final[tuple[str, ...]] = (
    "attach.json",
    "attach_result.json",
    "attach_result_error.json",
    "detach.json",
    "resize.json",
    "set_viewport.json",
    "set_scroll_offset.json",
    "scroll_offset_applied.json",
    "list.json",
    "create.json",
    "create_result.json",
    "kill.json",
    "input.json",
    "write_outcome.json",
    "write_outcome_indeterminate.json",
    "write_outcome_refused.json",
    "write_outcome_conflict.json",
    "write_outcome_expired.json",
    "write_outcome_capacity.json",
    "output.json",
    "attach_history.json",
    "fragment.json",
    "fragment_last.json",
    "paste.json",
    "take_control.json",
    "release_control.json",
    "control_result.json",
    "lease_lost.json",
    "attachment_finalized.json",
    "event.json",
    "typed_error.json",
)

TERMINAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ATTACHMENT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class SafeIntegerOverflowError(ValueError):
    """Raised when a protocol counter is outside JavaScript's safe integer range."""

    def __init__(self) -> None:
        super().__init__("safe_integer_overflow")


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


def _fragment_dict(
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
    index: int,
    more: bool,
    chunk: bytes,
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
        "payload": base64.b64encode(chunk).decode("ascii"),
    }


def _greedy_chunks(
    complete_json: bytes,
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
) -> list[bytes]:
    chunks: list[bytes] = []
    offset = 0
    while offset < len(complete_json):
        remaining = len(complete_json) - offset
        take = remaining
        while take > 0:
            chunk = complete_json[offset : offset + take]
            probe = _fragment_dict(
                event=event,
                terminal_id=terminal_id,
                attachment_id=attachment_id,
                message_seq=message_seq,
                index=len(chunks),
                more=offset + take < len(complete_json),
                chunk=chunk,
            )
            if len(canonical_json(probe)) < TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES:
                chunks.append(chunk)
                offset += take
                break
            take = max(1, take // 2)
            if take == 1 and len(canonical_json(probe)) >= TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES:
                raise ValueError("fragment_too_large")
        else:
            raise ValueError("fragment_too_large")
    return chunks


def fragment_event(
    *,
    event: str,
    terminal_id: str,
    attachment_id: str,
    message_seq: int,
    complete_json: bytes,
) -> list[dict[str, Any]]:
    """Split a complete JSON payload into terminal_ws_fragment slices."""
    _check_safe_int(message_seq, "message_seq")
    if len(complete_json) > TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES:
        raise ValueError("fragment_too_large")
    midpoint = max(1, len(complete_json) // 2)
    slices = [complete_json[:midpoint], complete_json[midpoint:]]
    if any(
        len(
            canonical_json(
                _fragment_dict(
                    event=event,
                    terminal_id=terminal_id,
                    attachment_id=attachment_id,
                    message_seq=message_seq,
                    index=index,
                    more=index < len(slices) - 1,
                    chunk=chunk,
                )
            )
        )
        >= TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES
        for index, chunk in enumerate(slices)
        if chunk
    ):
        slices = _greedy_chunks(
            complete_json,
            event=event,
            terminal_id=terminal_id,
            attachment_id=attachment_id,
            message_seq=message_seq,
        )
    fragments: list[dict[str, Any]] = []
    for index, chunk in enumerate(slices):
        if not chunk and index > 0:
            continue
        fragments.append(
            _fragment_dict(
                event=event,
                terminal_id=terminal_id,
                attachment_id=attachment_id,
                message_seq=message_seq,
                index=index,
                more=index < len(slices) - 1,
                chunk=chunk,
            )
        )
    return fragments


def emit_proxied_event(
    event: Mapping[str, Any],
    *,
    message_seq: int,
) -> list[dict[str, Any]]:
    """Send unfragmented when canonical JSON is under 2 MiB, else fragment."""
    payload = dict(event)
    raw = canonical_json(payload)
    if len(raw) > TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES:
        raise ValueError("fragment_too_large")
    if len(raw) < TERMINAL_WS_FRAGMENT_MAX_WRAPPED_BYTES:
        return [payload]
    return fragment_event(
        event=str(payload["type"]),
        terminal_id=str(payload["terminal_id"]),
        attachment_id=str(payload["attachment_id"]),
        message_seq=message_seq,
        complete_json=raw,
    )


def golden_fixtures() -> dict[str, dict[str, Any]]:
    """Canonical objects committed as tests/servers/fixtures/terminal_ws_golden/."""
    history = {
        "type": "terminal_attach_history",
        "terminal_id": TERMINAL_ID,
        "attachment_id": ATTACHMENT_ID,
        "text": "ready.\n",
        "truncated": False,
        "dropped_bytes": 0,
        "total_bytes": 7,
    }
    history_bytes = encode_message(history)
    fragments = fragment_event(
        event="terminal_attach_history",
        terminal_id=TERMINAL_ID,
        attachment_id=ATTACHMENT_ID,
        message_seq=1,
        complete_json=history_bytes,
    )
    return {
        "attach.json": {
            "type": "terminal_attach",
            "request_id": "req-attach-1",
            "terminal_id": TERMINAL_ID,
            "frame_delivery": "proxy",
        },
        "attach_result.json": {
            "type": "terminal_attach_result",
            "request_id": "req-attach-1",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "rows": 24,
            "cols": 80,
            "backend": "tmux",
            "frame_delivery": "proxy",
            "lease_generation": 0,
        },
        "attach_result_error.json": {
            "type": "terminal_attach_result",
            "request_id": "req-attach-1",
            "terminal_id": TERMINAL_ID,
            "success": False,
            "code": "terminal_gone",
        },
        "detach.json": {
            "type": "terminal_detach",
            "request_id": "req-detach-1",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
        },
        "resize.json": {
            "type": "terminal_resize",
            "request_id": "req-resize-1",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "rows": 24,
            "cols": 80,
        },
        "set_viewport.json": {
            "type": "terminal_set_viewport",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "rows": 24,
            "cols": 80,
        },
        "set_scroll_offset.json": {
            "type": "terminal_set_scroll_offset",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "rows_from_live_edge": 12,
        },
        "scroll_offset_applied.json": {
            "type": "terminal_scroll_offset_applied",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "applied_rows": 12,
            "max_rows": 40,
        },
        "list.json": {
            "type": "terminal_list",
            "request_id": "req-list-1",
            "limit": 100,
            "items": [
                {
                    "terminal_id": TERMINAL_ID,
                    "backend": "tmux",
                    "ownership": "gobby",
                    "state": "live",
                    "title": "sess",
                    "session_id": None,
                    "agent_run_id": None,
                    "dims": {"rows": 24, "cols": 80},
                }
            ],
            "next_cursor": None,
        },
        "create.json": {
            "type": "terminal_create",
            "request_id": "req-create-1",
            "rows": 24,
            "cols": 80,
            "cwd": "/tmp",
            "command": ["zsh"],
        },
        "create_result.json": {
            "type": "terminal_create_result",
            "request_id": "req-create-1",
            "success": True,
            "terminal_id": TERMINAL_ID,
            "backend": "tmux",
        },
        "kill.json": {
            "type": "terminal_kill",
            "request_id": "req-kill-1",
            "terminal_id": TERMINAL_ID,
        },
        "input.json": {
            "type": "terminal_input",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "data": "ls\n",
            "client_write_seq": 1,
        },
        "write_outcome.json": {
            "type": "terminal_write_outcome",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "client_write_seq": 1,
            "outcome": "delivered",
            "reason": None,
        },
        "write_outcome_indeterminate.json": {
            "type": "terminal_write_outcome",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "client_write_seq": 2,
            "outcome": "indeterminate",
            "reason": "indeterminate_backend",
        },
        "write_outcome_refused.json": {
            "type": "terminal_write_outcome",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "client_write_seq": 3,
            "outcome": "refused",
            "reason": "held",
        },
        "write_outcome_conflict.json": {
            "type": "terminal_write_outcome",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "client_write_seq": 4,
            "outcome": "refused",
            "reason": "write_seq_conflict",
        },
        "write_outcome_expired.json": {
            "type": "terminal_write_outcome",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "client_write_seq": 5,
            "outcome": "refused",
            "reason": "write_seq_expired",
        },
        "write_outcome_capacity.json": {
            "type": "terminal_write_outcome",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "client_write_seq": 6,
            "outcome": "refused",
            "reason": "write_seq_capacity",
        },
        "output.json": {
            "type": "terminal_output",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "data": "ready.\n",
        },
        "attach_history.json": history,
        "fragment.json": fragments[0],
        "fragment_last.json": fragments[1],
        "paste.json": {
            "type": "terminal_paste",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "text": "hello",
            "client_write_seq": 7,
        },
        "take_control.json": {
            "type": "terminal_take_control",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "takeover": False,
        },
        "release_control.json": {
            "type": "terminal_release_control",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
        },
        "control_result.json": {
            "type": "terminal_control_result",
            "attachment_id": ATTACHMENT_ID,
            "granted": True,
            "reason": None,
            "lease_generation": 1,
        },
        "lease_lost.json": {
            "type": "terminal_lease_lost",
            "attachment_id": ATTACHMENT_ID,
            "holder": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "lease_generation": 2,
        },
        "attachment_finalized.json": {
            "type": "terminal_attachment_finalized",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
            "reason": "detach",
            "lease_generation": 1,
        },
        "event.json": {
            "type": "terminal_event",
            "event": "created",
            "terminal_id": TERMINAL_ID,
        },
        "typed_error.json": {
            "type": "terminal_error",
            "code": "held",
            "terminal_id": TERMINAL_ID,
            "attachment_id": ATTACHMENT_ID,
        },
    }


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


def inventory_item(row: Any) -> dict[str, Any]:
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
        "dims": dims,
    }


def encode_page(items: Sequence[Mapping[str, Any]], next_cursor: str | None) -> dict[str, Any]:
    """Build a paginated inventory payload, cutting to the encoded-byte budget."""
    selected: list[Mapping[str, Any]] = []
    cursor = next_cursor
    for item in items:
        candidate = [*selected, item]
        payload = {"items": candidate, "next_cursor": None}
        if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > (
            TERMINAL_LIST_MAX_ENCODED_BYTES
        ):
            cursor = str(selected[-1]["id"]) if selected else None
            break
        selected.append(item)
    else:
        cursor = next_cursor
    return {"items": list(selected), "next_cursor": cursor}
