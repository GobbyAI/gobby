"""Derive whether the current turn began with a provider-recorded interrupt."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from gobby.hooks.events import SessionSource
from gobby.sessions.transcript_cursor import (
    CLAUDE_INTERRUPT_PREFIX,
    CLAUDE_USER_REJECTED,
)

__all__ = ["turn_interrupt_initiated"]

_TRANSCRIPT_TAIL_BYTES = 256 * 1024
_ClaudeRecordKind = Literal["assistant", "interrupt", "other", "prompt"]
_CodexMarker = Literal["interrupt", "prompt"]


def _tail_records(transcript_path: str | Path | None) -> list[dict[str, Any]]:
    if transcript_path is None or not str(transcript_path).strip():
        return []

    try:
        with Path(transcript_path).expanduser().open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            start = max(0, size - _TRANSCRIPT_TAIL_BYTES)
            stream.seek(start)
            tail = stream.read(_TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return []

    if start:
        _, separator, tail = tail.partition(b"\n")
        if not separator:
            return []

    records: list[dict[str, Any]] = []
    for line in tail.splitlines():
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _claude_content(record: dict[str, Any]) -> Any:
    message = record.get("message")
    return message.get("content") if isinstance(message, dict) else None


def _claude_record_kind(record: dict[str, Any]) -> _ClaudeRecordKind | None:
    record_type = record.get("type")
    if record_type == "assistant":
        return "assistant"
    if record_type != "user":
        return None
    if record.get("toolDenialKind") == CLAUDE_USER_REJECTED:
        return "interrupt"

    content = _claude_content(record)
    if isinstance(content, str):
        if content.startswith(CLAUDE_INTERRUPT_PREFIX):
            return "interrupt"
        return "prompt"
    if not isinstance(content, list):
        return "other"

    text_blocks = [
        block for block in content if isinstance(block, dict) and block.get("type") == "text"
    ]
    if any(str(block.get("text", "")).startswith(CLAUDE_INTERRUPT_PREFIX) for block in text_blocks):
        return "interrupt"
    if any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content):
        return None
    return "prompt" if text_blocks else "other"


def _claude_turn_interrupt_initiated(records: list[dict[str, Any]]) -> bool:
    kinds = [
        kind for record in reversed(records) if (kind := _claude_record_kind(record)) is not None
    ]
    for index, kind in enumerate(kinds):
        if kind == "assistant":
            continue
        if kind == "interrupt":
            return True
        if kind == "prompt":
            return index + 1 < len(kinds) and kinds[index + 1] == "interrupt"
        return False
    return False


def _codex_marker(record: dict[str, Any]) -> _CodexMarker | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if record.get("type") == "event_msg":
        payload_type = payload.get("type")
        if payload_type == "turn_aborted":
            return "interrupt"
        if payload_type == "task_started":
            return "prompt"
    return None


def _codex_turn_interrupt_initiated(records: list[dict[str, Any]]) -> bool:
    event_markers = [marker for record in records if (marker := _codex_marker(record)) is not None]
    if "prompt" in event_markers:
        return len(event_markers) >= 2 and event_markers[-2:] == ["interrupt", "prompt"]

    fallback_markers: list[_CodexMarker] = []
    for record in records:
        marker = _codex_marker(record)
        payload = record.get("payload")
        if (
            marker is None
            and record.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "message"
            and payload.get("role") == "user"
        ):
            marker = "prompt"
        if marker is not None:
            fallback_markers.append(marker)
    return len(fallback_markers) >= 2 and fallback_markers[-2:] == ["interrupt", "prompt"]


def turn_interrupt_initiated(
    source: SessionSource | str | None,
    transcript_path: str | Path | None,
) -> bool:
    """Return a fresh per-turn interrupt fact derived from the provider transcript."""
    source_value = source.value if isinstance(source, SessionSource) else source
    if source_value not in {SessionSource.CLAUDE.value, SessionSource.CODEX.value}:
        return False
    records = _tail_records(transcript_path)
    if source_value == SessionSource.CLAUDE.value:
        return _claude_turn_interrupt_initiated(records)
    return _codex_turn_interrupt_initiated(records)
