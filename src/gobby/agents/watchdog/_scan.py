"""Shared JSONL scanner for provider-specific watchdog readers."""

import json
import os
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import BinaryIO, Self, cast

from gobby.agents.watchdog.models import (
    WATCHDOG_TAIL_LIMIT,
    TranscriptEventSummary,
    WatchdogTranscriptSnapshot,
)

_SCAN_STATE_LIMIT = 128
_RESUME_GUARD_BYTES = 256


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


@dataclass
class ResumableScanState:
    """Classifier state through the last newline-terminated line of one transcript.

    Providers subclass it with their own fields, `classify` and snapshot.
    """

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
    transcript size (#23359), so each read resumes after the last complete line.
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
