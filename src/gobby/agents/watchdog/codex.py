"""Codex rollout reader for provider-neutral watchdog signals."""

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

CODEX_MODEL_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_stored_datetime(value)
    except (TypeError, ValueError):
        return None


def _response_activity_kind(payload_type: str, payload: dict[str, object]) -> ActivityKind:
    if payload_type == "reasoning":
        return "reasoning"
    if payload_type == "message":
        return "user_input" if payload.get("role") == "user" else "message"
    if payload_type in {
        "custom_tool_call",
        "custom_tool_call_output",
        "function_call",
        "function_call_output",
        "web_search_call",
    }:
        return "tool"
    return "other"


def _read_codex_snapshot(path: str) -> WatchdogTranscriptSnapshot:
    tail: deque[TranscriptEventSummary] = deque(maxlen=WATCHDOG_TAIL_LIMIT)
    turn_started_event: TranscriptEventSummary | None = None
    latest_turn_event: TranscriptEventSummary | None = None
    latest_turn_kind: TurnEventKind | None = None
    provider_error_event: TranscriptEventSummary | None = None
    provider_error_reason: str | None = None
    latest_activity_kind: ActivityKind | None = None
    latest_model_output_line_num: int | None = None

    def classify(line_num: int, data: dict[str, object]) -> ScanVerdict:
        nonlocal turn_started_event
        nonlocal latest_turn_event
        nonlocal latest_turn_kind
        nonlocal provider_error_event
        nonlocal provider_error_reason
        nonlocal latest_activity_kind
        nonlocal latest_model_output_line_num

        event_type = data.get("type")
        if not isinstance(event_type, str):
            return ScanVerdict.MALFORMED
        if event_type not in {"response_item", "event_msg"}:
            return ScanVerdict.IGNORED
        payload_value = data.get("payload")
        if not isinstance(payload_value, dict):
            return ScanVerdict.MALFORMED
        payload = payload_value
        payload_type = payload.get("type")
        if not isinstance(payload_type, str):
            return ScanVerdict.MALFORMED
        if event_type == "event_msg" and payload_type == "context_compacted":
            return ScanVerdict.IGNORED

        summary = TranscriptEventSummary(
            line_num=line_num,
            timestamp=_parse_timestamp(data.get("timestamp")),
            event_type=event_type,
            payload_type=payload_type,
        )
        if event_type == "response_item":
            tail.append(summary)
            latest_activity_kind = _response_activity_kind(payload_type, payload)
            if payload_type != "message" or payload.get("role") != "user":
                latest_model_output_line_num = line_num
            return ScanVerdict.VALID

        if payload_type in {"agent_message", "agent_reasoning"}:
            latest_model_output_line_num = line_num
        if payload_type == "task_started":
            turn_started_event = summary
            latest_turn_event = summary
            latest_turn_kind = "started"
        elif payload_type == "task_complete":
            latest_turn_event = summary
            latest_turn_kind = "completed"
        elif payload_type == "turn_aborted":
            latest_turn_event = summary
            latest_turn_kind = "aborted"
        if (
            payload_type == "error"
            and payload.get("message") == CODEX_MODEL_CAPACITY_MESSAGE
            and payload.get("codex_error_info") == "server_overloaded"
        ):
            provider_error_event = summary
            provider_error_reason = "server_overloaded"
        return ScanVerdict.VALID

    result = scan_jsonl(path, classify)
    return WatchdogTranscriptSnapshot(
        provider="codex",
        tail=tuple(tail),
        turn_started_event=turn_started_event,
        latest_turn_event=latest_turn_event,
        latest_turn_kind=latest_turn_kind,
        provider_error_event=provider_error_event,
        provider_error_kind="capacity" if provider_error_event is not None else None,
        provider_error_reason=provider_error_reason,
        latest_activity_kind=latest_activity_kind,
        latest_model_output_line_num=latest_model_output_line_num,
        last_malformed_line_num=result.last_malformed_line_num,
    )


class CodexTranscriptWatchdogReader:
    provider_id: str = "codex"
    capacity_pane_message: str | None = CODEX_MODEL_CAPACITY_MESSAGE
    supports_reasoning_interrupt: bool = True

    async def read(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        return await asyncio.to_thread(_read_codex_snapshot, transcript_path)


CODEX_WATCHDOG_READER = CodexTranscriptWatchdogReader()
