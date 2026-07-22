"""Structurally redacted Droid transcript diagnostics for the idle watchdog."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime

from gobby.agents.watchdog._scan import ScanVerdict, scan_jsonl
from gobby.agents.watchdog.models import (
    WATCHDOG_TAIL_LIMIT,
    ActivityKind,
    TranscriptEventSummary,
    WatchdogTranscriptSnapshot,
)
from gobby.utils.datetime import parse_stored_datetime

_DROID_RECORD_TYPES = frozenset({"message", "session_end", "session_start", "todo_state"})
_DROID_ROLES = frozenset({"assistant", "user"})


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_stored_datetime(value)
    except (TypeError, ValueError):
        return None


def _message_shape(data: dict[str, object]) -> tuple[str, tuple[str, ...]] | None:
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    content = message.get("content")
    if not isinstance(role, str) or role not in _DROID_ROLES or not isinstance(content, list):
        return None

    block_types: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            return None
        block_type = block.get("type")
        if not isinstance(block_type, str):
            return None
        block_types.append(block_type)
    return role, tuple(block_types)


def _payload_type(block_types: tuple[str, ...]) -> str:
    if "thinking" in block_types:
        return "thinking"
    if "tool_use" in block_types:
        return "tool_use"
    if "tool_result" in block_types:
        return "tool_result"
    if "text" in block_types:
        return "text"
    return "other"


def _assistant_activity_kind(block_types: tuple[str, ...]) -> ActivityKind:
    if "thinking" in block_types:
        return "reasoning"
    if "tool_use" in block_types or "tool_result" in block_types:
        return "tool"
    if "text" in block_types:
        return "message"
    return "other"


def _read_droid_snapshot(path: str) -> WatchdogTranscriptSnapshot:
    tail: deque[TranscriptEventSummary] = deque(maxlen=WATCHDOG_TAIL_LIMIT)
    latest_activity_kind: ActivityKind | None = None
    latest_model_output_line_num: int | None = None

    def classify(line_num: int, data: dict[str, object]) -> ScanVerdict:
        nonlocal latest_activity_kind
        nonlocal latest_model_output_line_num

        record_type = data.get("type")
        if not isinstance(record_type, str):
            return ScanVerdict.MALFORMED
        if record_type not in _DROID_RECORD_TYPES:
            return ScanVerdict.IGNORED

        timestamp = _parse_timestamp(data.get("timestamp"))
        if record_type == "session_start":
            tail.append(
                TranscriptEventSummary(
                    line_num=line_num,
                    timestamp=timestamp,
                    event_type="session_start",
                    payload_type="task_started",
                )
            )
            return ScanVerdict.VALID
        if record_type == "session_end":
            tail.append(
                TranscriptEventSummary(
                    line_num=line_num,
                    timestamp=timestamp,
                    event_type="session_end",
                    payload_type="task_complete",
                )
            )
            return ScanVerdict.VALID
        if record_type == "todo_state":
            todos = data.get("todos")
            valid_todos = isinstance(todos, list) or (
                isinstance(todos, dict) and isinstance(todos.get("todos"), str)
            )
            if not valid_todos:
                return ScanVerdict.MALFORMED
            tail.append(
                TranscriptEventSummary(
                    line_num=line_num,
                    timestamp=timestamp,
                    event_type="system",
                    payload_type="todo_state",
                )
            )
            return ScanVerdict.VALID

        shape = _message_shape(data)
        if shape is None:
            return ScanVerdict.MALFORMED
        role, block_types = shape
        tail.append(
            TranscriptEventSummary(
                line_num=line_num,
                timestamp=timestamp,
                event_type=role,
                payload_type=_payload_type(block_types),
            )
        )
        if role == "assistant":
            latest_activity_kind = _assistant_activity_kind(block_types)
            latest_model_output_line_num = line_num
        else:
            latest_activity_kind = "user_input"
        return ScanVerdict.VALID

    result = scan_jsonl(path, classify)
    return WatchdogTranscriptSnapshot(
        provider="droid",
        tail=tuple(tail),
        latest_activity_kind=latest_activity_kind,
        latest_model_output_line_num=latest_model_output_line_num,
        last_malformed_line_num=result.last_malformed_line_num,
    )


class DroidTranscriptWatchdogReader:
    provider_id: str = "droid"
    capacity_pane_message: str | None = None
    supports_reasoning_interrupt: bool = False

    async def read(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        return await asyncio.to_thread(_read_droid_snapshot, transcript_path)


DROID_WATCHDOG_READER = DroidTranscriptWatchdogReader()


async def read_droid_transcript_snapshot(path: str) -> WatchdogTranscriptSnapshot:
    """Read structurally redacted Droid watchdog diagnostics from JSONL."""
    return await DROID_WATCHDOG_READER.read(path)
