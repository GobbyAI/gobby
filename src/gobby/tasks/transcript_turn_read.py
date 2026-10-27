"""Read only a live transcript's latest turn for the found-work stop gate (#25143).

A stop reads backward from the end in bounded chunks and stops at the latest user
prompt, at the boundary the previous clean stop stored, or at the byte cap. Its cost
follows the turn, never the transcript.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import IO, Any

from gobby.sessions.transcripts.base import ParsedMessage, RawLine, TranscriptParser

CHUNK_BYTES = 64 * 1024
MAX_TURN_BYTES = 1024 * 1024
# Bytes ending at a stored offset; a mismatch means the file was replaced.
_TAIL_BYTES = 1024


@dataclass(frozen=True)
class TurnRead:
    lines: list[RawLine]
    # Where the next stop resumes, or None when no complete line was read.
    boundary: dict[str, Any] | None


def read_latest_turn(
    path: str, boundary: object, new_parser: Callable[[], TranscriptParser]
) -> TurnRead:
    """Return the complete lines from the turn's start to the last newline."""
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        floor = _resume_offset(handle, path, size, boundary)
        line_start = floor or 0
        lower = max(line_start, size - MAX_TURN_BYTES)
        pos, end = size, None
        head = b""  # bytes from pos not yet split into complete lines
        chunks: list[list[RawLine]] = []
        start_at: int | None = None
        while pos > lower and start_at is None:
            start = max(lower, pos - CHUNK_BYTES)
            data = _read_exact(handle, start, pos - start) + head
            pos = start
            if end is None:
                cut = data.rfind(b"\n") + 1
                if not cut:
                    head = data
                    continue
                end = start + cut
                data = data[:cut]
            skip = 0 if start == line_start else data.find(b"\n") + 1
            if start != line_start and not skip:
                head = data
                continue
            head = data[:skip]
            lines = _split_lines(data[skip:], start + skip)
            chunks.append(lines)
            start_at = _latest_prompt(new_parser(), lines)
        if end is None:
            return TurnRead(lines=[], boundary=None)
        tail = _read_exact(handle, max(0, end - _TAIL_BYTES), min(end, _TAIL_BYTES))
    lines = [line for chunk in reversed(chunks) for line in chunk]
    if start_at is not None:
        lines = [line for line in lines if (line.byte_offset or 0) >= start_at]
    return TurnRead(
        lines=lines,
        boundary={"path": path, "offset": end, "tail_sha256": hashlib.sha256(tail).hexdigest()},
    )


def _resume_offset(handle: IO[bytes], path: str, size: int, boundary: object) -> int | None:
    """The stored offset when it still names this file's bytes, else None."""
    if not isinstance(boundary, Mapping) or boundary.get("path") != path:
        return None
    offset = boundary.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 < offset <= size:
        return None
    tail = _read_exact(handle, max(0, offset - _TAIL_BYTES), min(offset, _TAIL_BYTES))
    return offset if hashlib.sha256(tail).hexdigest() == boundary.get("tail_sha256") else None


def _read_exact(handle: IO[bytes], start: int, length: int) -> bytes:
    handle.seek(start)
    data = handle.read(length)
    if len(data) != length:
        raise OSError(f"transcript shrank while reading {length} bytes at {start}")
    return data


def _split_lines(data: bytes, offset: int) -> list[RawLine]:
    lines: list[RawLine] = []
    for number, chunk in enumerate(data.splitlines(keepends=True)):
        lines.append(RawLine(byte_offset=offset, raw_line_no=number, text=chunk.decode("utf-8")))
        offset += len(chunk)
    return lines


def _latest_prompt(parser: TranscriptParser, lines: list[RawLine]) -> int | None:
    latest = None
    for event in parser.iter_parse_events(lines):
        if event.byte_offset is not None and any(
            isinstance(record, ParsedMessage)
            and record.role == "user"
            and record.content_type == "text"
            and str(record.content).strip()
            for record in event.records
        ):
            latest = event.byte_offset
    return latest


__all__ = ["CHUNK_BYTES", "MAX_TURN_BYTES", "TurnRead", "read_latest_turn"]
