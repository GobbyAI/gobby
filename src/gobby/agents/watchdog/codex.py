"""Codex rollout reader for provider-neutral watchdog signals."""

import asyncio
import json
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import cast

from gobby.agents.watchdog._scan import ScanVerdict
from gobby.agents.watchdog.models import (
    WATCHDOG_TAIL_LIMIT,
    ActivityKind,
    TranscriptEventSummary,
    TurnEventKind,
    WatchdogTranscriptSnapshot,
)
from gobby.utils.datetime import parse_stored_datetime

CODEX_MODEL_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
_CODEX_SCAN_STATE_LIMIT = 128
_CODEX_RESUME_GUARD_BYTES = 256


@dataclass
class _CodexScanState:
    device: int
    inode: int
    offset: int = 0
    line_num: int = 0
    guard_start: int = 0
    resume_guard: bytes = b""
    tail: deque[TranscriptEventSummary] = field(
        default_factory=lambda: deque(maxlen=WATCHDOG_TAIL_LIMIT)
    )
    turn_started_event: TranscriptEventSummary | None = None
    latest_turn_event: TranscriptEventSummary | None = None
    latest_turn_kind: TurnEventKind | None = None
    provider_error_event: TranscriptEventSummary | None = None
    provider_error_reason: str | None = None
    latest_activity_kind: ActivityKind | None = None
    latest_model_output_line_num: int | None = None
    last_malformed_line_num: int | None = None


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


def _read_codex_snapshot(
    path: str,
    states: OrderedDict[str, _CodexScanState] | None = None,
) -> WatchdogTranscriptSnapshot:
    transcript_path = Path(path)
    stat = transcript_path.stat()
    state = states.get(path) if states is not None else None
    guard_matches = True
    if state is not None and state.resume_guard:
        with transcript_path.open("rb") as guard_handle:
            guard_handle.seek(state.guard_start)
            guard_matches = guard_handle.read(len(state.resume_guard)) == state.resume_guard
    if (
        state is None
        or state.device != stat.st_dev
        or state.inode != stat.st_ino
        or state.offset > stat.st_size
        or not guard_matches
    ):
        state = _CodexScanState(device=stat.st_dev, inode=stat.st_ino)
        if states is not None:
            states[path] = state

    def classify(line_num: int, data: dict[str, object]) -> ScanVerdict:
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
            state.tail.append(summary)
            state.latest_activity_kind = _response_activity_kind(payload_type, payload)
            if payload_type != "message" or payload.get("role") != "user":
                state.latest_model_output_line_num = line_num
            return ScanVerdict.VALID

        if payload_type in {"agent_message", "agent_reasoning"}:
            state.latest_model_output_line_num = line_num
        if payload_type == "task_started":
            state.turn_started_event = summary
            state.latest_turn_event = summary
            state.latest_turn_kind = "started"
        elif payload_type == "task_complete":
            state.latest_turn_event = summary
            state.latest_turn_kind = "completed"
        elif payload_type == "turn_aborted":
            state.latest_turn_event = summary
            state.latest_turn_kind = "aborted"
        if (
            payload_type == "error"
            and payload.get("message") == CODEX_MODEL_CAPACITY_MESSAGE
            and payload.get("codex_error_info") == "server_overloaded"
        ):
            state.provider_error_event = summary
            state.provider_error_reason = "server_overloaded"
        return ScanVerdict.VALID

    with transcript_path.open("rb") as handle:
        handle.seek(state.offset)
        while True:
            record_offset = handle.tell()
            raw_line = handle.readline()
            if not raw_line:
                break
            if not raw_line.strip():
                state.line_num += 1
                continue
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if not raw_line.endswith(b"\n"):
                    handle.seek(record_offset)
                    break
                state.line_num += 1
                state.last_malformed_line_num = state.line_num
                continue

            state.line_num += 1
            if not isinstance(value, dict):
                state.last_malformed_line_num = state.line_num
                continue
            verdict = classify(state.line_num, cast(dict[str, object], value))
            if verdict is ScanVerdict.MALFORMED:
                state.last_malformed_line_num = state.line_num
        state.offset = handle.tell()
        state.guard_start = max(0, state.offset - _CODEX_RESUME_GUARD_BYTES)
        handle.seek(state.guard_start)
        state.resume_guard = handle.read(state.offset - state.guard_start)

    return WatchdogTranscriptSnapshot(
        provider="codex",
        tail=tuple(state.tail),
        turn_started_event=state.turn_started_event,
        latest_turn_event=state.latest_turn_event,
        latest_turn_kind=state.latest_turn_kind,
        provider_error_event=state.provider_error_event,
        provider_error_kind="capacity" if state.provider_error_event is not None else None,
        provider_error_reason=state.provider_error_reason,
        latest_activity_kind=state.latest_activity_kind,
        latest_model_output_line_num=state.latest_model_output_line_num,
        last_malformed_line_num=state.last_malformed_line_num,
    )


class CodexTranscriptWatchdogReader:
    provider_id: str = "codex"
    capacity_pane_message: str | None = CODEX_MODEL_CAPACITY_MESSAGE
    supports_reasoning_interrupt: bool = True

    def __init__(self) -> None:
        self._states: OrderedDict[str, _CodexScanState] = OrderedDict()
        self._state_lock = Lock()

    def _read_snapshot(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        with self._state_lock:
            snapshot = _read_codex_snapshot(transcript_path, self._states)
            self._states.move_to_end(transcript_path)
            while len(self._states) > _CODEX_SCAN_STATE_LIMIT:
                self._states.popitem(last=False)
            return snapshot

    async def read(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        return await asyncio.to_thread(self._read_snapshot, transcript_path)


CODEX_WATCHDOG_READER = CodexTranscriptWatchdogReader()
