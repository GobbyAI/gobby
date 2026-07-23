"""Watchdog signal extraction for Grok JSON-RPC update transcripts."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from datetime import UTC, datetime
from typing import TypeGuard

from gobby.agents.watchdog._scan import ScanVerdict, scan_jsonl
from gobby.agents.watchdog.models import (
    WATCHDOG_TAIL_LIMIT,
    ActivityKind,
    TranscriptEventSummary,
    TurnEventKind,
    WatchdogTranscriptSnapshot,
)

_UPDATE_METHODS = frozenset(
    {
        "session/update",
        "_x.ai/session/update",
        "_x.ai/session_notification",
    }
)
_CHUNK_UPDATE_TYPES = frozenset(
    {
        "agent_message_chunk",
        "agent_thought_chunk",
        "user_message_chunk",
    }
)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_epoch(value: object, *, divisor: int = 1) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = value / divisor
    if not math.isfinite(seconds):
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_timestamp(data: dict[str, object], params: dict[str, object]) -> datetime | None:
    timestamp = _parse_epoch(data.get("timestamp"))
    if timestamp is not None:
        return timestamp
    metadata = params.get("_meta")
    if not isinstance(metadata, dict):
        return None
    return _parse_epoch(metadata.get("agentTimestampMs"), divisor=1000)


def _valid_turn_completed(update: dict[str, object]) -> bool:
    return isinstance(update.get("prompt_id"), str) and isinstance(
        update.get("stop_reason"),
        str,
    )


def _valid_retry_state(update: dict[str, object]) -> bool:
    attempt = update.get("attempt")
    max_retries = update.get("max_retries")
    return (
        update.get("type") == "retrying"
        and _is_int(attempt)
        and attempt >= 0
        and _is_int(max_retries)
        and max_retries > 0
        and attempt <= max_retries
        and isinstance(update.get("reason"), str)
    )


def _activity_kind(update_type: str) -> ActivityKind | None:
    if update_type == "agent_message_chunk":
        return "message"
    if update_type == "agent_thought_chunk":
        return "reasoning"
    if update_type == "user_message_chunk":
        return "user_input"
    if update_type.startswith("tool_call"):
        return "tool"
    return None


def _read_grok_snapshot(path: str) -> WatchdogTranscriptSnapshot:
    tail: deque[TranscriptEventSummary] = deque(maxlen=WATCHDOG_TAIL_LIMIT)
    turn_started_event: TranscriptEventSummary | None = None
    latest_turn_event: TranscriptEventSummary | None = None
    latest_turn_kind: TurnEventKind | None = None
    provider_error_event: TranscriptEventSummary | None = None
    latest_activity_kind: ActivityKind | None = None
    latest_model_output_line_num: int | None = None

    def classify(line_num: int, data: dict[str, object]) -> ScanVerdict:
        nonlocal turn_started_event
        nonlocal latest_turn_event
        nonlocal latest_turn_kind
        nonlocal provider_error_event
        nonlocal latest_activity_kind
        nonlocal latest_model_output_line_num

        method = data.get("method")
        if not isinstance(method, str) or method not in _UPDATE_METHODS:
            return ScanVerdict.IGNORED
        params_value = data.get("params")
        if not isinstance(params_value, dict):
            return ScanVerdict.MALFORMED
        params = params_value
        update_value = params.get("update")
        if not isinstance(update_value, dict):
            return ScanVerdict.MALFORMED
        update = update_value
        update_type = update.get("sessionUpdate")
        if not isinstance(update_type, str):
            return ScanVerdict.MALFORMED

        is_tool_update = update_type.startswith("tool_call")
        if (
            update_type not in _CHUNK_UPDATE_TYPES
            and update_type not in {"turn_completed", "retry_state"}
            and not is_tool_update
        ):
            return ScanVerdict.IGNORED
        if update_type in _CHUNK_UPDATE_TYPES and not isinstance(update.get("content"), dict):
            return ScanVerdict.MALFORMED
        if update_type == "turn_completed" and not _valid_turn_completed(update):
            return ScanVerdict.MALFORMED
        if update_type == "retry_state" and not _valid_retry_state(update):
            return ScanVerdict.MALFORMED

        summary = TranscriptEventSummary(
            line_num=line_num,
            timestamp=_parse_timestamp(data, params),
            event_type="session_update",
            payload_type=update_type,
        )
        tail.append(summary)

        activity_kind = _activity_kind(update_type)
        if activity_kind is not None:
            latest_activity_kind = activity_kind
        if activity_kind in {"message", "reasoning", "tool"}:
            latest_model_output_line_num = line_num
        if update_type == "user_message_chunk":
            turn_started_event = summary
            latest_turn_event = summary
            latest_turn_kind = "started"
        elif update_type == "turn_completed":
            latest_turn_event = summary
            latest_turn_kind = "completed"
        elif update_type == "retry_state":
            provider_error_event = summary
        return ScanVerdict.VALID

    result = scan_jsonl(path, classify)
    return WatchdogTranscriptSnapshot(
        provider="grok",
        tail=tuple(tail),
        turn_started_event=turn_started_event,
        latest_turn_event=latest_turn_event,
        latest_turn_kind=latest_turn_kind,
        provider_error_event=provider_error_event,
        provider_error_kind="retry" if provider_error_event is not None else None,
        provider_error_reason="retrying" if provider_error_event is not None else None,
        latest_activity_kind=latest_activity_kind,
        latest_model_output_line_num=latest_model_output_line_num,
        last_malformed_line_num=result.last_malformed_line_num,
    )


class GrokTranscriptWatchdogReader:
    provider_id: str = "grok"
    capacity_pane_message: str | None = None
    supports_reasoning_interrupt: bool = False

    async def read(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        return await asyncio.to_thread(_read_grok_snapshot, transcript_path)


GROK_WATCHDOG_READER = GrokTranscriptWatchdogReader()
