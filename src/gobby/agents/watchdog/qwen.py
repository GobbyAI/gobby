"""Structurally redacted Qwen transcript diagnostics for the idle watchdog."""

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

_QWEN_MESSAGE_TYPES = frozenset({"assistant", "tool_result", "user"})
_QWEN_RECORD_TYPES = _QWEN_MESSAGE_TYPES | {"system"}
_QWEN_SYSTEM_PAYLOADS = {
    "file_history_snapshot": "snapshot",
    "ui_telemetry": "telemetry",
}


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_stored_datetime(value)
    except (TypeError, ValueError):
        return None


def _part_types(data: dict[str, object]) -> tuple[str, ...] | None:
    message = data.get("message")
    if not isinstance(message, dict):
        return None
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None

    part_types: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            return None
        thought = part.get("thought")
        if "thought" in part and not isinstance(thought, bool):
            return None
        if "functionCall" in part:
            if not isinstance(part.get("functionCall"), dict):
                return None
            part_types.append("tool_call")
        elif "functionResponse" in part:
            if not isinstance(part.get("functionResponse"), dict):
                return None
            part_types.append("tool_result")
        elif "text" in part:
            if not isinstance(part.get("text"), str):
                return None
            part_types.append("thinking" if thought is True else "text")
        else:
            part_types.append("other")
    return tuple(part_types)


def _payload_type(record_type: str, part_types: tuple[str, ...]) -> str:
    if record_type == "tool_result":
        return "tool_result"
    if "thinking" in part_types:
        return "thinking"
    if "tool_call" in part_types:
        return "tool_call"
    if "tool_result" in part_types:
        return "tool_result"
    if "text" in part_types:
        return "text"
    return "other"


def _activity_kind(record_type: str, part_types: tuple[str, ...]) -> ActivityKind:
    if record_type == "user":
        return "user_input"
    if record_type == "tool_result":
        return "tool"
    if "thinking" in part_types:
        return "reasoning"
    if "tool_call" in part_types or "tool_result" in part_types:
        return "tool"
    if "text" in part_types:
        return "message"
    return "other"


def _read_qwen_snapshot(path: str) -> WatchdogTranscriptSnapshot:
    tail: deque[TranscriptEventSummary] = deque(maxlen=WATCHDOG_TAIL_LIMIT)
    latest_activity_kind: ActivityKind | None = None
    latest_model_output_line_num: int | None = None

    def classify(line_num: int, data: dict[str, object]) -> ScanVerdict:
        nonlocal latest_activity_kind
        nonlocal latest_model_output_line_num

        record_type = data.get("type")
        if not isinstance(record_type, str):
            return ScanVerdict.MALFORMED
        if record_type not in _QWEN_RECORD_TYPES:
            return ScanVerdict.IGNORED

        timestamp = _parse_timestamp(data.get("timestamp"))
        if record_type == "system":
            subtype = data.get("subtype")
            if not isinstance(subtype, str):
                return ScanVerdict.MALFORMED
            payload_type = _QWEN_SYSTEM_PAYLOADS.get(subtype)
            if payload_type is None:
                return ScanVerdict.IGNORED
            tail.append(
                TranscriptEventSummary(
                    line_num=line_num,
                    timestamp=timestamp,
                    event_type="system",
                    payload_type=payload_type,
                )
            )
            return ScanVerdict.VALID

        part_types = _part_types(data)
        if part_types is None:
            return ScanVerdict.MALFORMED
        tail.append(
            TranscriptEventSummary(
                line_num=line_num,
                timestamp=timestamp,
                event_type=record_type if record_type != "tool_result" else "message",
                payload_type=_payload_type(record_type, part_types),
            )
        )
        latest_activity_kind = _activity_kind(record_type, part_types)
        if record_type == "assistant":
            latest_model_output_line_num = line_num
        return ScanVerdict.VALID

    result = scan_jsonl(path, classify)
    return WatchdogTranscriptSnapshot(
        provider="qwen",
        tail=tuple(tail),
        latest_activity_kind=latest_activity_kind,
        latest_model_output_line_num=latest_model_output_line_num,
        last_malformed_line_num=result.last_malformed_line_num,
    )


class QwenTranscriptWatchdogReader:
    provider_id: str = "qwen"
    capacity_pane_message: str | None = None
    supports_reasoning_interrupt: bool = False

    async def read(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        return await asyncio.to_thread(_read_qwen_snapshot, transcript_path)


QWEN_WATCHDOG_READER = QwenTranscriptWatchdogReader()
