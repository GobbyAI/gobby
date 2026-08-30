"""Transcript conversion helpers routed through the shared parser registry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcripts import get_parser
from gobby.sessions.transcripts.base import NON_MESSAGE_CONTENT_TYPES, ParsedMessage


def _parse_lines(
    lines: list[str],
    source: str,
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> list[ParsedMessage]:
    """Parse lines into ParsedMessage objects."""
    parser = get_parser(source, session_id=session_id, transcript_path=transcript_path)
    parsed = parser.parse_lines(lines, start_index=0)
    normalized = normalize_transcript_records(parsed, source)
    return [r for r in normalized if isinstance(r, ParsedMessage)]


def _parse_lines_to_dicts(
    lines: list[str],
    source: str,
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Parse JSONL lines through the appropriate transcript parser."""
    parsed = _parse_lines(lines, source, session_id=session_id, transcript_path=transcript_path)
    return _parsed_to_dicts(parsed)


def _parsed_to_dicts(parsed: list[ParsedMessage]) -> list[dict[str, Any]]:
    """Convert ParsedMessage list to dicts."""
    results: list[dict[str, Any]] = []
    for msg in parsed:
        if msg.content_type in NON_MESSAGE_CONTENT_TYPES:
            # Session metadata is not a conversation message — never flattened.
            continue
        results.append(
            {
                "session_id": None,
                "message_index": msg.index,
                "role": msg.role,
                "content": msg.content,
                "content_type": msg.content_type,
                "tool_name": msg.tool_name,
                "tool_input": msg.tool_input,
                "tool_result": msg.tool_result,
                "tool_use_id": msg.tool_use_id,
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                "raw_json": msg.raw_json,
            }
        )
    return results
