"""Tail cursors that confirm a CLI interrupt from its own transcript.

Before the daemon types a handoff command it interrupts the running turn. A CLI
that did not actually stop queues the typed command as the next user message, so
the interrupt is confirmed from fresh transcript records rather than assumed:
Codex appends ``turn_aborted`` to its rollout, Claude Code appends a user record
carrying ``[Request interrupted by user...]`` or a user-rejected tool result.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

__all__ = [
    "CLAUDE_INTERRUPT_PREFIX",
    "CLAUDE_USER_REJECTED",
    "INTERRUPT_OBSERVED_SOURCES",
    "ClaudeTranscriptCursor",
    "CodexRolloutCursor",
    "InterruptObserver",
    "TranscriptObservationError",
    "TranscriptTailCursor",
    "build_interrupt_observer",
]

logger = logging.getLogger(__name__)

InterruptObserver = Callable[[], bool | None]

# CLIs whose transcripts record interrupts; every other CLI keeps the blind path.
INTERRUPT_OBSERVED_SOURCES = frozenset({"claude", "codex"})

CLAUDE_INTERRUPT_PREFIX = "[Request interrupted by user"
CLAUDE_USER_REJECTED = "user-rejected"


class TranscriptObservationError(RuntimeError):
    """Raised when a transcript can no longer be observed safely."""


@dataclass
class TranscriptTailCursor:
    """Read only fresh, complete JSONL records appended to a stable transcript."""

    path: Path
    device: int
    inode: int
    offset: int
    _buffer: bytes = field(default=b"", repr=False)
    _discard_historical_partial: bool = field(default=False, repr=False)

    @classmethod
    def at_eof(cls, transcript_path: str | Path | None) -> Self:
        """Create a cursor at the current EOF of a readable transcript."""
        if transcript_path is None or not str(transcript_path).strip():
            raise TranscriptObservationError("session has no transcript path")

        path = Path(transcript_path).expanduser()
        try:
            with path.open("rb") as stream:
                file_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise TranscriptObservationError(f"transcript is not a regular file: {path}")
                discard_historical_partial = False
                if file_stat.st_size:
                    stream.seek(-1, os.SEEK_END)
                    discard_historical_partial = stream.read(1) != b"\n"
        except TranscriptObservationError:
            raise
        except OSError as exc:
            raise TranscriptObservationError(f"transcript is unavailable: {path}: {exc}") from exc

        return cls(
            path=path,
            device=file_stat.st_dev,
            inode=file_stat.st_ino,
            offset=file_stat.st_size,
            _discard_historical_partial=discard_historical_partial,
        )

    def fresh_records(self) -> Iterator[dict[str, Any]]:
        """Yield each complete JSON object record appended since the last read."""
        chunk = self._read_appended_bytes()
        if not chunk:
            return

        if self._discard_historical_partial:
            newline = chunk.find(b"\n")
            if newline < 0:
                return
            chunk = chunk[newline + 1 :]
            self._discard_historical_partial = False

        records = (self._buffer + chunk).split(b"\n")
        self._buffer = records.pop()
        for raw_record in records:
            try:
                record = json.loads(raw_record.rstrip(b"\r"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(record, dict):
                yield record

    def _read_appended_bytes(self) -> bytes:
        try:
            path_stat = self.path.stat()
            self._validate_stat(path_stat)
            with self.path.open("rb") as stream:
                opened_stat = os.fstat(stream.fileno())
                self._validate_stat(opened_stat)
                stream.seek(self.offset)
                chunk = stream.read()
                read_stat = os.fstat(stream.fileno())
            final_path_stat = self.path.stat()
            self._validate_stat(read_stat, minimum_size=self.offset + len(chunk))
            self._validate_stat(final_path_stat, minimum_size=self.offset + len(chunk))
        except TranscriptObservationError:
            raise
        except OSError as exc:
            raise TranscriptObservationError(
                f"transcript became unavailable: {self.path}: {exc}"
            ) from exc

        self.offset += len(chunk)
        return chunk

    def _validate_stat(self, file_stat: os.stat_result, *, minimum_size: int | None = None) -> None:
        if file_stat.st_dev != self.device or file_stat.st_ino != self.inode:
            raise TranscriptObservationError(f"transcript was replaced: {self.path}")
        required_size = self.offset if minimum_size is None else minimum_size
        if file_stat.st_size < required_size:
            raise TranscriptObservationError(f"transcript was truncated: {self.path}")


@dataclass
class CodexRolloutCursor(TranscriptTailCursor):
    """Codex rollout cursor: a fresh ``turn_aborted`` event confirms the interrupt."""

    def saw_fresh_turn_aborted(self) -> bool:
        """Return whether newly appended complete records contain turn_aborted."""
        for record in self.fresh_records():
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "turn_aborted":
                return True
        return False


@dataclass
class ClaudeTranscriptCursor(TranscriptTailCursor):
    """Claude Code transcript cursor: an interrupted-user record confirms the interrupt."""

    def saw_fresh_interrupt(self) -> bool:
        """Return whether newly appended records show the turn was interrupted."""
        for record in self.fresh_records():
            if record.get("type") != "user":
                continue
            if record.get("toolDenialKind") == CLAUDE_USER_REJECTED:
                return True
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                if content.startswith(CLAUDE_INTERRUPT_PREFIX):
                    return True
                continue
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and str(block.get("text", "")).startswith(CLAUDE_INTERRUPT_PREFIX)
                ):
                    return True
        return False


def build_interrupt_observer(
    source: str | None,
    transcript_path: str | Path | None,
    *,
    session_id: str | None,
) -> InterruptObserver | None:
    """Return an interrupt observer for CLIs whose transcripts record interrupts.

    Returns ``None`` for CLIs without an observable transcript. Raises
    ``TranscriptObservationError`` when the transcript of an observable CLI
    cannot be opened, so callers fail closed instead of typing blind.
    """
    if source not in INTERRUPT_OBSERVED_SOURCES:
        return None
    observe: Callable[[], bool]
    if source == "codex":
        observe = CodexRolloutCursor.at_eof(transcript_path).saw_fresh_turn_aborted
    else:
        observe = ClaudeTranscriptCursor.at_eof(transcript_path).saw_fresh_interrupt

    def observe_interrupt() -> bool | None:
        try:
            return observe()
        except TranscriptObservationError as exc:
            logger.warning(
                "Lost %s interrupt observation for session %s: %s",
                source,
                session_id,
                exc,
            )
            return None

    return observe_interrupt
