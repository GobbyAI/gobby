"""Shared JSONL scanner for provider-specific watchdog readers."""

import json
import os
import re
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import BinaryIO, ClassVar, Self, cast

from gobby.agents.watchdog.models import (
    WATCHDOG_TAIL_LIMIT,
    TranscriptEventSummary,
    WatchdogTranscriptSnapshot,
)

_SCAN_STATE_LIMIT = 128
_RESUME_GUARD_BYTES = 256
_TURN_SEARCH_CHUNK_BYTES = 1 << 20
_SCAN_POSITION_FIELDS = frozenset(
    {"device", "inode", "offset", "line_num", "guard_start", "resume_guard"}
)


class ScanVerdict(StrEnum):
    VALID = "valid"
    IGNORED = "ignored"
    MALFORMED = "malformed"


ClassifyRecord = Callable[[int, dict[str, object]], ScanVerdict]


@dataclass(frozen=True, slots=True)
class ScanResult:
    last_malformed_line_num: int | None = None


def scan_line_is_malformed(line_num: int, raw_line: bytes, classify: ClassifyRecord) -> bool:
    """Decode one non-blank JSONL line, classify it, and report whether it is malformed."""
    try:
        value = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return True
    if not isinstance(value, dict):
        return True
    return classify(line_num, cast(dict[str, object], value)) is ScanVerdict.MALFORMED


def scan_jsonl(path: str | Path, classify: ClassifyRecord) -> ScanResult:
    """Decode JSONL records and delegate provider-specific shape validation."""
    last_malformed_line_num: int | None = None
    with Path(path).open("rb") as handle:
        for line_num, raw_line in enumerate(handle, start=1):
            if raw_line.strip() and scan_line_is_malformed(line_num, raw_line, classify):
                last_malformed_line_num = line_num
    return ScanResult(last_malformed_line_num=last_malformed_line_num)


def find_turn_start(
    handle: BinaryIO,
    size: int,
    marker: re.Pattern[bytes],
    starts_turn: Callable[[dict[str, object]], bool],
) -> tuple[int, int] | None:
    """Offset and preceding line count of the latest complete line that starts a turn.

    Scan state is turn-scoped, so a cold scan from the latest turn start matches a scan
    of the whole transcript; decoding every earlier line held the daemon GIL in
    proportion to transcript size (#23359). The marker only nominates lines: each
    candidate is decoded and confirmed, so the result is never past the true start.
    """
    buf = b""
    pos = size
    while pos > 0:
        start = max(0, pos - _TURN_SEARCH_CHUNK_BYTES)
        handle.seek(start)
        buf = handle.read(pos - start) + buf
        pos = start
        # The line before the first newline may continue into unread bytes, and a record
        # after the last newline may still be unterminated.
        first = 0 if pos == 0 else buf.find(b"\n") + 1
        if first == 0 and pos > 0:
            continue
        decoded_from = end = buf.rfind(b"\n") + 1
        for match in reversed(list(marker.finditer(buf, first, end))):
            line_start = buf.rfind(b"\n", 0, match.start()) + 1
            if line_start >= decoded_from:
                continue
            decoded_from = line_start
            line_end = buf.index(b"\n", match.end()) + 1
            try:
                value = json.loads(buf[line_start:line_end].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and starts_turn(cast(dict[str, object], value)):
                offset = pos + line_start
                return offset, _count_newlines(handle, offset)
        buf = buf[:first]
    return None


def _count_newlines(handle: BinaryIO, end: int) -> int:
    handle.seek(0)
    count = 0
    remaining = end
    while remaining > 0:
        chunk = handle.read(min(_TURN_SEARCH_CHUNK_BYTES, remaining))
        if not chunk:
            break
        count += chunk.count(b"\n")
        remaining -= len(chunk)
    return count


@dataclass
class ResumableScanState:
    """Classifier state through the last newline-terminated line of one transcript.

    Providers subclass it with their own fields, `classify`, snapshot and turn-start
    marker, and call `begin_turn` from `classify` when a record starts a turn.
    """

    turn_start_marker: ClassVar[re.Pattern[bytes]]

    device: int
    inode: int
    offset: int = 0
    line_num: int = 0
    guard_start: int = 0
    resume_guard: bytes = b""
    tail: deque[TranscriptEventSummary] = field(
        default_factory=lambda: deque(maxlen=WATCHDOG_TAIL_LIMIT)
    )
    last_malformed_line_num: int | None = None

    def classify(self, line_num: int, data: dict[str, object]) -> ScanVerdict:
        raise NotImplementedError

    def snapshot(self) -> WatchdogTranscriptSnapshot:
        raise NotImplementedError

    def starts_turn(self, data: dict[str, object]) -> bool:
        """Whether `classify` takes this record as a turn start, judged on a fresh state."""
        probe = type(self)(self.device, self.inode)
        probe.classify(1, data)
        return probe.snapshot().turn_started_event is not None

    def begin_turn(self) -> None:
        """Forget what earlier turns left, keeping only the scan position.

        Every verdict is judged within the latest turn, and a cold scan starts there
        (`find_turn_start`), so no state may depend on the lines before it.
        """
        fresh = type(self)(self.device, self.inode)
        for item in fields(self):
            if item.name not in _SCAN_POSITION_FIELDS:
                setattr(self, item.name, getattr(fresh, item.name))

    def resumes(self, handle: BinaryIO, stat: os.stat_result) -> bool:
        """Whether the open file is the one scanned so far, changed only by appends.

        Only the bytes just before the kept offset are compared, so an in-place rewrite
        of the same inode that keeps them resumes wrongly; transcripts are append-only.
        """
        if (self.device, self.inode) != (stat.st_dev, stat.st_ino) or self.offset > stat.st_size:
            return False
        handle.seek(self.guard_start)
        return handle.read(len(self.resume_guard)) == self.resume_guard

    def scan_line(self, line_num: int, raw_line: bytes) -> None:
        if scan_line_is_malformed(line_num, raw_line, self.classify):
            self.last_malformed_line_num = line_num

    def pending(self) -> Self:
        """A copy that can count a record still being written without changing this state."""
        return replace(self, tail=deque(self.tail, maxlen=WATCHDOG_TAIL_LIMIT))


class ResumableScanner[S: ResumableScanState]:
    """Per-path scan states that decode only the lines appended since the last read.

    Decoding the whole transcript on every poll held the daemon GIL in proportion to
    transcript size (#23359), so each read resumes after the last complete line and a
    new state starts at the latest turn start.
    """

    def __init__(self, new_state: Callable[[int, int], S]) -> None:
        self._new_state = new_state
        self._states: OrderedDict[str, S] = OrderedDict()
        self._lock = Lock()

    def snapshot(self, path: str) -> WatchdogTranscriptSnapshot:
        with self._lock:
            snapshot = self._scan(path).snapshot()
            self._states.move_to_end(path)
            while len(self._states) > _SCAN_STATE_LIMIT:
                self._states.popitem(last=False)
            return snapshot

    def _scan(self, path: str) -> S:
        with Path(path).open("rb") as handle:
            stat = os.fstat(handle.fileno())
            state = self._states.get(path)
            if state is None or not state.resumes(handle, stat):
                state = self._new_state(stat.st_dev, stat.st_ino)
                turn_start = find_turn_start(
                    handle, stat.st_size, state.turn_start_marker, state.starts_turn
                )
                if turn_start is not None:
                    state.offset, state.line_num = turn_start
                self._states[path] = state
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
            state.guard_start = max(0, state.offset - _RESUME_GUARD_BYTES)
            handle.seek(state.guard_start)
            state.resume_guard = handle.read(state.offset - state.guard_start)
        if not unterminated.strip():
            return state
        # A record still being written counts as it stands, on a copy, so the next read
        # decodes it again from the kept offset once its newline lands.
        pending = state.pending()
        pending.scan_line(state.line_num + 1, unterminated)
        return pending
