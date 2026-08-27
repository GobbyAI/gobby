"""Transcript and digest helpers for session summary generation."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, NamedTuple

from gobby.sessions.analyzer_turns import SUMMARY_ANALYZER_MAX_RECORDS
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import (
    ParsedMessage,
    RawLine,
    TranscriptReadError,
    decode_transcript_record,
)
from gobby.utils.injected_context import strip_injected_context

logger = logging.getLogger("gobby.sessions.summarize")

TURN_PATTERN = re.compile(r"^### Turn \d+", re.MULTILINE)
TRANSCRIPT_FALLBACK_MAX_TURNS = 80
TRANSCRIPT_FALLBACK_MAX_CHARS = 24_000
DIGEST_FALLBACK_MAX_CHARS = 24_000
TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS = 0.2
TRANSCRIPT_TAIL_CHUNK_BYTES = 65_536


class TranscriptWindow(NamedTuple):
    turns: list[dict[str, Any]]
    truncated: bool


_HANDOFF_RETRIEVAL = (
    "get_handoff_context (gobby-sessions) retrieves the full stored content for the current "
    "session.\n... [truncated]"
)


async def _read_transcript(
    path: Path,
    source: str = "claude",
    max_turns: int | None = None,
) -> list[dict[str, Any]]:
    """Read and parse a transcript file in its native format.

    Claude, Codex, Droid, and Qwen use JSONL (one JSON object per line).
    The returned dicts are in the source's native format - callers that need
    to iterate content blocks should use format-aware helpers.

    Args:
        path: Path to the transcript file.
        source: Session source (``"claude"``, ``"qwen"``, ``"codex"``,
            ``"droid"``).
    """
    return (await _read_transcript_window(path, source=source, max_records=max_turns)).turns


async def _read_transcript_window(
    path: Path,
    *,
    source: str = "claude",
    max_records: int | None = SUMMARY_ANALYZER_MAX_RECORDS,
) -> TranscriptWindow:
    """Read the newest complete records with bounded reverse I/O."""
    _ = source
    window, tail_withheld = await asyncio.to_thread(_read_transcript_window_once, path, max_records)
    if not tail_withheld:
        return window
    await asyncio.sleep(TRANSCRIPT_TAIL_RETRY_DELAY_SECONDS)
    window, _ = await asyncio.to_thread(_read_transcript_window_once, path, max_records)
    return window


def _read_transcript_window_once(
    path: Path, max_records: int | None
) -> tuple[TranscriptWindow, bool]:
    with path.open("rb") as transcript:
        transcript.seek(0, 2)
        file_size = transcript.tell()
        if max_records is None:
            transcript.seek(0)
            data = transcript.read()
            positioned = [
                (raw, offset) for raw, offset in _positioned_records(data, 0) if raw.strip()
            ]
            truncated = False
        else:
            positioned, truncated = _read_positioned_tail(transcript, file_size, max_records)

    records: list[dict[str, Any]] = []
    for raw_record, byte_offset in positioned:
        record = decode_transcript_record(
            raw_record,
            path=path,
            byte_offset=byte_offset,
            line_number=None,
            is_final=byte_offset + len(raw_record) == file_size,
        )
        if record is None:
            return TranscriptWindow(records, truncated), True
        records.append(record)
    return TranscriptWindow(records, truncated), False


def _read_positioned_tail(
    transcript: Any, file_size: int, max_records: int
) -> tuple[list[tuple[bytes, int]], bool]:
    if max_records <= 0:
        return [], file_size > 0
    position = file_size
    target = max_records + 1
    positioned_reverse: list[tuple[bytes, int]] = []
    pending_parts: list[bytes] = []

    while position > 0 and len(positioned_reverse) < target:
        read_size = min(TRANSCRIPT_TAIL_CHUNK_BYTES, position)
        position -= read_size
        transcript.seek(position)
        chunk = transcript.read(read_size)
        cursor = len(chunk)
        while len(positioned_reverse) < target:
            newline = chunk.rfind(b"\n", 0, cursor)
            if newline < 0:
                pending_parts.append(chunk[:cursor])
                break
            raw_record = b"".join([chunk[newline + 1 : cursor], *reversed(pending_parts)])
            if raw_record.strip():
                positioned_reverse.append((raw_record, position + newline + 1))
            pending_parts = [b"\n"]
            cursor = newline

    if position == 0 and len(positioned_reverse) < target:
        raw_record = b"".join(reversed(pending_parts))
        if raw_record.strip():
            positioned_reverse.append((raw_record, 0))

    truncated = len(positioned_reverse) > max_records
    return list(reversed(positioned_reverse[:max_records])), truncated


def _positioned_records(data: bytes, start_offset: int) -> list[tuple[bytes, int]]:
    positioned: list[tuple[bytes, int]] = []
    offset = start_offset
    for raw_record in data.splitlines(keepends=True):
        positioned.append((raw_record, offset))
        offset += len(raw_record)
    return positioned


async def _read_first_user_goal(
    path: Path,
    *,
    source: str,
    max_records: int = SUMMARY_ANALYZER_MAX_RECORDS,
) -> str | None:
    """Return the first normalized user text within a bounded forward scan."""
    return await asyncio.to_thread(
        _scan_first_user_goal,
        path,
        source,
        max_records,
    )


def _scan_first_user_goal(path: Path, source: str, max_records: int) -> str | None:
    parser = get_parser(source, transcript_path=path)
    for event in parser.iter_parse_events(_raw_lines_from_file(path, max_records)):
        for record in event.records:
            if (
                isinstance(record, ParsedMessage)
                and record.role == "user"
                and record.content_type == "text"
            ):
                text = _parsed_content_text(record.content)
                if text:
                    return text
    return None


def _raw_lines_from_file(path: Path, max_records: int) -> Iterator[RawLine]:
    with path.open("rb") as transcript:
        for raw_line_no in range(max_records):
            byte_offset = transcript.tell()
            raw_record = transcript.readline()
            if not raw_record:
                return
            try:
                text = raw_record.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TranscriptReadError(path, byte_offset, line_number=None) from exc
            yield RawLine(
                byte_offset=byte_offset,
                raw_line_no=raw_line_no,
                text=text,
            )


def _parsed_content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        text = value.get("text")
        return text.strip() if isinstance(text, str) else ""
    return ""


def _summary_source_text(value: str | None) -> str:
    """Normalize optional markdown fields for summary context decisions."""
    if not isinstance(value, str) or not value.strip():
        return ""
    return strip_injected_context(value).strip()


def _digest_markdown_for_summary(session: Any) -> str:
    """Return digest context with the latest completed turn when digest lags."""
    digest_markdown = strip_injected_context(
        _summary_source_text(getattr(session, "digest_markdown", None))
    )
    pending_turns = [
        strip_injected_context(_summary_source_text(getattr(session, "last_turn_markdown", None))),
        strip_injected_context(
            _summary_source_text(getattr(session, "last_assistant_content", None))
        ),
    ]

    summary_parts = [digest_markdown] if digest_markdown else []
    next_turn = len(TURN_PATTERN.findall(digest_markdown)) + 1
    for turn_markdown in pending_turns:
        if not turn_markdown:
            continue
        joined_summary = "\n\n".join(summary_parts)
        if turn_markdown in joined_summary:
            continue
        summary_parts.append(f"### Turn {next_turn}\n{turn_markdown}")
        next_turn += 1

    return "\n\n".join(summary_parts)


def _strip_injected_context_from_value(value: Any) -> Any:
    if isinstance(value, str):
        return strip_injected_context(value)
    if isinstance(value, list):
        return [_strip_injected_context_from_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _strip_injected_context_from_value(item) for key, item in value.items()}
    return value


def _truncate_markdown(value: str, max_chars: int) -> str:
    """Bound inline context and identify how to retrieve the stored full content."""
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    if max_chars <= len(_HANDOFF_RETRIEVAL):
        return _HANDOFF_RETRIEVAL[:max_chars]
    separator = "\n\n"
    head_chars = max_chars - len(separator) - len(_HANDOFF_RETRIEVAL)
    return f"{value[:head_chars]}{separator}{_HANDOFF_RETRIEVAL}"


def _format_transcript_fallback_summary(
    turns: list[dict[str, Any]],
    formatter: Any,
) -> str:
    """Format a bounded transcript fallback for sessions without digest markdown."""
    bounded_turns = turns[-TRANSCRIPT_FALLBACK_MAX_TURNS:]
    formatted = formatter(bounded_turns)
    return _truncate_markdown(formatted, TRANSCRIPT_FALLBACK_MAX_CHARS)


def _format_deterministic_summary(handoff_ctx: Any, digest_markdown: str) -> str:
    """Build deterministic markdown when provider generation is unavailable."""
    from gobby.sessions.formatting import format_handoff_as_markdown

    base_markdown = format_handoff_as_markdown(handoff_ctx)
    current_state_parts: list[str] = []
    if digest_markdown:
        digest_section = _truncate_markdown(digest_markdown, DIGEST_FALLBACK_MAX_CHARS)
        current_state_parts.append(f"### Session Digest\n\n{digest_section}")
    if base_markdown:
        current_state_parts.append(base_markdown)
    if not current_state_parts:
        return ""

    current_state = "\n\n".join(current_state_parts)
    return (
        f"## Current State\n\n{current_state}\n\n"
        "## Next Steps\n\nContinue from the captured session state."
    )


async def async_enumerate[T](
    aiter: AsyncIterator[T], start: int = 0
) -> AsyncIterator[tuple[int, T]]:
    """Async version of enumerate."""
    idx = start
    async for item in aiter:
        yield idx, item
        idx += 1


def _extract_digest_turns(digest_markdown: str | None) -> tuple[str, str]:
    """Extract first and last digest turns from rolling digest markdown.

    Args:
        digest_markdown: The session's rolling digest_markdown field.

    Returns:
        Tuple of (first_turn_text, recent_turns_text). Empty strings if unavailable.
    """
    if not digest_markdown:
        return "", ""

    # Split on ### Turn N headings
    parts = TURN_PATTERN.split(digest_markdown)
    headings = TURN_PATTERN.findall(digest_markdown)

    if not headings:
        # No turn structure - return first 500 chars as first turn
        return _truncate_markdown(digest_markdown.strip(), 500), ""

    # parts[0] is content before first heading (preamble), parts[1:] are turn contents
    # Pair headings with their content
    turns: list[str] = []
    for i, heading in enumerate(headings):
        content = parts[i + 1] if (i + 1) < len(parts) else ""
        turns.append(f"{heading}\n{content.strip()}")

    first_turn = turns[0] if turns else ""
    # Last 2 turns for recent context
    recent = turns[-2:] if len(turns) >= 2 else turns
    recent_turns = "\n\n".join(recent)

    # Truncate to avoid blowing up the prompt
    if len(first_turn) > 800:
        first_turn = _truncate_markdown(first_turn, 800)
    if len(recent_turns) > 1500:
        recent_turns = _truncate_markdown(recent_turns, 1500)

    return first_turn, recent_turns
