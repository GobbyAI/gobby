"""Transcript parser dispatch and conversion helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcripts.base import ParsedMessage

if TYPE_CHECKING:
    from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
    from gobby.sessions.transcripts.codex import CodexTranscriptParser
    from gobby.sessions.transcripts.droid import DroidTranscriptParser
    from gobby.sessions.transcripts.grok import GrokTranscriptParser
    from gobby.sessions.transcripts.qwen import QwenTranscriptParser

    TranscriptParser = (
        ClaudeTranscriptParser
        | GrokTranscriptParser
        | QwenTranscriptParser
        | CodexTranscriptParser
        | DroidTranscriptParser
    )


def _get_parser(
    source: str,
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> TranscriptParser:
    """Get the appropriate transcript parser for a source."""
    from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
    from gobby.sessions.transcripts.codex import CodexTranscriptParser
    from gobby.sessions.transcripts.droid import DroidTranscriptParser
    from gobby.sessions.transcripts.grok import GrokTranscriptParser
    from gobby.sessions.transcripts.qwen import QwenTranscriptParser

    if source == "grok":
        return GrokTranscriptParser(session_id=session_id)
    if source == "qwen":
        return QwenTranscriptParser(session_id=session_id)
    if source == "codex":
        return CodexTranscriptParser(session_id=session_id)
    if source == "droid":
        return DroidTranscriptParser(session_id=session_id, transcript_path=transcript_path)
    return ClaudeTranscriptParser(session_id=session_id)


def _parse_lines(
    lines: list[str],
    source: str,
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> list[ParsedMessage]:
    """Parse lines into ParsedMessage objects."""
    parser = _get_parser(source, session_id=session_id, transcript_path=transcript_path)
    parsed = parser.parse_lines(lines, start_index=0)
    normalized = normalize_transcript_records(parsed, source)
    return [r for r in normalized if isinstance(r, ParsedMessage)]


def _parse_json_session(
    data: dict[str, Any],
    source: str,
    session_id: str | None = None,
    transcript_path: str | Path | None = None,
) -> list[ParsedMessage]:
    """Parse a native JSON session file."""
    from gobby.sessions.transcripts.qwen import QwenTranscriptParser

    if source == "qwen":
        qwen_parser = QwenTranscriptParser(session_id=session_id)
        return [
            r
            for r in normalize_transcript_records(qwen_parser.parse_session_json(data), source)
            if isinstance(r, ParsedMessage)
        ]
    return _parse_lines(
        [json.dumps(data)],
        source,
        session_id=session_id,
        transcript_path=transcript_path,
    )


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
