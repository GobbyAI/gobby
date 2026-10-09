"""Structurally redacted watchdog signals from Claude Code JSONL transcripts."""

import asyncio
import os
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from gobby.agents.watchdog._scan import ScanVerdict, scan_line_is_malformed
from gobby.agents.watchdog.models import (
    WATCHDOG_TAIL_LIMIT,
    ActivityKind,
    TranscriptEventSummary,
    TurnEventKind,
    WatchdogTranscriptSnapshot,
)
from gobby.utils.datetime import parse_stored_datetime

_CLAUDE_RECORD_TYPES = frozenset({"assistant", "system", "user"})
_AUTH_API_ERROR_LABELS = frozenset({"authentication_failed", "oauth_org_not_allowed"})
_MAX_API_ERROR_MESSAGE_CHARS = 160
_CLAUDE_SCAN_STATE_LIMIT = 128
_CLAUDE_RESUME_GUARD_BYTES = 256


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_stored_datetime(value)
    except (TypeError, ValueError):
        return None


def _content_block_types(
    data: dict[str, object],
    *,
    allow_string: bool,
    expected_role: str,
) -> tuple[str, ...] | None:
    message = data.get("message")
    if not isinstance(message, dict) or message.get("role") != expected_role:
        return None
    content = message.get("content")
    if allow_string and isinstance(content, str):
        return ()
    if not isinstance(content, list):
        return None
    block_types: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            return None
        block_type = block.get("type")
        if not isinstance(block_type, str):
            return None
        block_types.append(block_type)
    return tuple(block_types)


def _assistant_activity_kind(block_types: tuple[str, ...]) -> ActivityKind:
    if "thinking" in block_types:
        return "reasoning"
    if "tool_use" in block_types:
        return "tool"
    if "text" in block_types:
        return "message"
    return "other"


def _assistant_payload_type(
    block_types: tuple[str, ...],
    *,
    is_api_error: bool,
) -> str:
    if is_api_error:
        return "api_error"
    activity_kind = _assistant_activity_kind(block_types)
    return {
        "reasoning": "thinking",
        "tool": "tool_use",
        "message": "text",
        "other": "other",
    }[activity_kind]


def _valid_turn_duration(data: dict[str, object]) -> bool:
    duration_ms = data.get("durationMs")
    message_count = data.get("messageCount")
    return (
        isinstance(duration_ms, int)
        and not isinstance(duration_ms, bool)
        and duration_ms >= 0
        and isinstance(message_count, int)
        and not isinstance(message_count, bool)
        and message_count >= 0
    )


def _api_error_message(data: dict[str, object]) -> str:
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    text = " ".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
    printable = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(printable.split())[:_MAX_API_ERROR_MESSAGE_CHARS]


def _terminal_api_error_reason(data: dict[str, object]) -> str | None:
    """Describe an API error the run cannot recover from, or None.

    Claude Code writes the API error record only after its own retries stop, so an
    authentication failure (401/403) or a server error (5xx) ends the run. Unknown
    error labels are not retained; the kind comes from known labels or the status.
    """
    label = data.get("error")
    status = data.get("apiErrorStatus")
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    if isinstance(label, str) and label in _AUTH_API_ERROR_LABELS:
        kind = label
    elif status in (401, 403):
        kind = "authentication_failed"
    elif status is not None and 500 <= status <= 599:
        kind = "server_error"
    else:
        return None
    reason = kind if status is None else f"{kind} (HTTP {status})"
    message = _api_error_message(data)
    return f"{reason}: {message}" if message else reason


@dataclass
class _ClaudeScanState:
    """Classifier state through the last newline-terminated line of one transcript."""

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
    terminal_error_reason: str | None = None
    latest_activity_kind: ActivityKind | None = None
    latest_model_output_line_num: int | None = None
    last_malformed_line_num: int | None = None

    def resumes(self, handle: BinaryIO, stat: os.stat_result) -> bool:
        """Whether the open file is the one scanned so far, changed only by appends."""
        if (self.device, self.inode) != (stat.st_dev, stat.st_ino) or self.offset > stat.st_size:
            return False
        handle.seek(self.guard_start)
        return handle.read(len(self.resume_guard)) == self.resume_guard

    def scan_line(self, line_num: int, raw_line: bytes) -> None:
        if scan_line_is_malformed(line_num, raw_line, self.classify):
            self.last_malformed_line_num = line_num

    def classify(self, line_num: int, data: dict[str, object]) -> ScanVerdict:
        record_type = data.get("type")
        if not isinstance(record_type, str):
            return ScanVerdict.MALFORMED
        if record_type not in _CLAUDE_RECORD_TYPES:
            return ScanVerdict.IGNORED

        timestamp = _parse_timestamp(data.get("timestamp"))
        if record_type == "assistant":
            block_types = _content_block_types(
                data,
                allow_string=False,
                expected_role="assistant",
            )
            is_api_error = data.get("isApiErrorMessage", False)
            if block_types is None or not isinstance(is_api_error, bool):
                return ScanVerdict.MALFORMED
            summary = TranscriptEventSummary(
                line_num=line_num,
                timestamp=timestamp,
                event_type="assistant",
                payload_type=_assistant_payload_type(
                    block_types,
                    is_api_error=is_api_error,
                ),
            )
            self.tail.append(summary)
            self.latest_activity_kind = _assistant_activity_kind(block_types)
            self.latest_model_output_line_num = line_num
            if is_api_error:
                self.provider_error_event = summary
                self.terminal_error_reason = _terminal_api_error_reason(data)
            return ScanVerdict.VALID

        if record_type == "user":
            block_types = _content_block_types(
                data,
                allow_string=True,
                expected_role="user",
            )
            if block_types is None:
                return ScanVerdict.MALFORMED
            summary = TranscriptEventSummary(
                line_num=line_num,
                timestamp=timestamp,
                event_type="user",
                payload_type="tool_result" if "tool_result" in block_types else "message",
            )
            self.tail.append(summary)
            if "tool_result" not in block_types:
                self.turn_started_event = summary
                self.latest_turn_event = summary
                self.latest_turn_kind = "started"
                self.latest_activity_kind = "user_input"
            return ScanVerdict.VALID

        subtype = data.get("subtype")
        if not isinstance(subtype, str):
            return ScanVerdict.MALFORMED
        if subtype == "turn_duration" and not _valid_turn_duration(data):
            return ScanVerdict.MALFORMED
        summary = TranscriptEventSummary(
            line_num=line_num,
            timestamp=timestamp,
            event_type="system",
            payload_type=subtype,
        )
        self.tail.append(summary)
        if subtype == "turn_duration":
            self.latest_turn_event = summary
            self.latest_turn_kind = "completed"
        return ScanVerdict.VALID

    def snapshot(self) -> WatchdogTranscriptSnapshot:
        return WatchdogTranscriptSnapshot(
            provider="claude",
            tail=tuple(self.tail),
            turn_started_event=self.turn_started_event,
            latest_turn_event=self.latest_turn_event,
            latest_turn_kind=self.latest_turn_kind,
            provider_error_event=self.provider_error_event,
            provider_error_kind=(
                None
                if self.provider_error_event is None
                else "terminal"
                if self.terminal_error_reason is not None
                else "api_error"
            ),
            provider_error_reason=(
                None
                if self.provider_error_event is None
                else self.terminal_error_reason or "api_error"
            ),
            latest_activity_kind=self.latest_activity_kind,
            latest_model_output_line_num=self.latest_model_output_line_num,
            last_malformed_line_num=self.last_malformed_line_num,
        )


def _read_claude_snapshot(
    path: str,
    states: OrderedDict[str, _ClaudeScanState],
) -> WatchdogTranscriptSnapshot:
    # Decoding the whole transcript on every poll held the daemon GIL in proportion to
    # transcript size (#23359), so each read resumes after the last complete line.
    with Path(path).open("rb") as handle:
        stat = os.fstat(handle.fileno())
        state = states.get(path)
        if state is None or not state.resumes(handle, stat):
            state = _ClaudeScanState(device=stat.st_dev, inode=stat.st_ino)
            states[path] = state
        handle.seek(state.offset)
        unterminated = b""
        for raw_line in handle:
            if not raw_line.endswith(b"\n"):
                unterminated = raw_line
                break
            state.line_num += 1
            if raw_line.strip():
                state.scan_line(state.line_num, raw_line)
            state.offset += len(raw_line)
        state.guard_start = max(0, state.offset - _CLAUDE_RESUME_GUARD_BYTES)
        handle.seek(state.guard_start)
        state.resume_guard = handle.read(state.offset - state.guard_start)
    if not unterminated.strip():
        return state.snapshot()
    # A record still being written counts as it stands, on a copy, so the next read
    # decodes it again from the kept offset once its newline lands.
    pending = replace(state, tail=deque(state.tail, maxlen=WATCHDOG_TAIL_LIMIT))
    pending.scan_line(state.line_num + 1, unterminated)
    return pending.snapshot()


class ClaudeTranscriptWatchdogReader:
    provider_id: str = "claude"
    capacity_pane_message: str | None = None
    supports_reasoning_interrupt: bool = False

    def __init__(self) -> None:
        self._states: OrderedDict[str, _ClaudeScanState] = OrderedDict()
        self._state_lock = Lock()

    def _read_snapshot(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        with self._state_lock:
            snapshot = _read_claude_snapshot(transcript_path, self._states)
            self._states.move_to_end(transcript_path)
            while len(self._states) > _CLAUDE_SCAN_STATE_LIMIT:
                self._states.popitem(last=False)
            return snapshot

    async def read(self, transcript_path: str) -> WatchdogTranscriptSnapshot:
        return await asyncio.to_thread(self._read_snapshot, transcript_path)


CLAUDE_WATCHDOG_READER = ClaudeTranscriptWatchdogReader()
