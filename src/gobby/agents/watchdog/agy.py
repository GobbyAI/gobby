"""Structurally redacted AGY transcript diagnostics for the idle watchdog."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime

from gobby.agents.watchdog._scan import ScanVerdict, scan_jsonl
from gobby.agents.watchdog.models import (
    WATCHDOG_TAIL_LIMIT,
    ActivityKind,
    TranscriptEventSummary,
    TurnEventKind,
    WatchdogTranscriptSnapshot,
)
from gobby.utils.datetime import parse_stored_datetime

_SYSTEM_SOURCES = frozenset({"SYSTEM", "SYSTEM_SDK"})


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_stored_datetime(value)
    except (TypeError, ValueError):
        return None


def _read_agy_snapshot(path: str) -> WatchdogTranscriptSnapshot:
    tail: deque[TranscriptEventSummary] = deque(maxlen=WATCHDOG_TAIL_LIMIT)
    turn_started_event: TranscriptEventSummary | None = None
    latest_turn_event: TranscriptEventSummary | None = None
    latest_turn_kind: TurnEventKind | None = None
    latest_activity_kind: ActivityKind | None = None
    latest_model_output_line_num: int | None = None

    def classify(line_num: int, data: dict[str, object]) -> ScanVerdict:
        nonlocal turn_started_event
        nonlocal latest_turn_event
        nonlocal latest_turn_kind
        nonlocal latest_activity_kind
        nonlocal latest_model_output_line_num

        source = data.get("source")
        record_type = data.get("type")
        if not isinstance(source, str) or not isinstance(record_type, str):
            return ScanVerdict.MALFORMED
        if source in _SYSTEM_SOURCES:
            return ScanVerdict.IGNORED

        timestamp = _parse_timestamp(data.get("created_at"))
        if source == "USER_EXPLICIT" and record_type == "USER_INPUT":
            summary = TranscriptEventSummary(
                line_num=line_num,
                timestamp=timestamp,
                event_type="user",
                payload_type="message",
            )
            tail.append(summary)
            turn_started_event = summary
            latest_turn_event = summary
            latest_turn_kind = "started"
            latest_activity_kind = "user_input"
            return ScanVerdict.VALID

        if source != "MODEL":
            return ScanVerdict.IGNORED

        if record_type == "PLANNER_RESPONSE":
            thinking = data.get("thinking")
            content = data.get("content")
            tool_calls = data.get("tool_calls")
            has_tools = isinstance(tool_calls, list) and bool(tool_calls)
            payload_type = "text"
            activity: ActivityKind = "message"
            if isinstance(thinking, str) and thinking:
                payload_type = "thinking"
                activity = "reasoning"
            if has_tools:
                payload_type = "tool_call"
                activity = "tool"
            summary = TranscriptEventSummary(
                line_num=line_num,
                timestamp=timestamp,
                event_type="assistant",
                payload_type=payload_type,
            )
            tail.append(summary)
            latest_activity_kind = activity
            latest_model_output_line_num = line_num
            if not has_tools and isinstance(content, str) and content:
                latest_turn_event = summary
                latest_turn_kind = "completed"
            return ScanVerdict.VALID

        summary = TranscriptEventSummary(
            line_num=line_num,
            timestamp=timestamp,
            event_type="tool_result",
            payload_type="tool_result",
        )
        tail.append(summary)
        latest_activity_kind = "tool"
        return ScanVerdict.VALID

    result = scan_jsonl(path, classify)
    return WatchdogTranscriptSnapshot(
        provider="agy",
        tail=tuple(tail),
        turn_started_event=turn_started_event,
        latest_turn_event=latest_turn_event,
        latest_turn_kind=latest_turn_kind,
        latest_activity_kind=latest_activity_kind,
        latest_model_output_line_num=latest_model_output_line_num,
        last_malformed_line_num=result.last_malformed_line_num,
    )


class AgyTranscriptWatchdogReader:
    provider_id: str = "agy"
    capacity_pane_message: str | None = None
    supports_reasoning_interrupt: bool = False

    async def read(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        return await asyncio.to_thread(_read_agy_snapshot, transcript_path)


AGY_WATCHDOG_READER = AgyTranscriptWatchdogReader()
