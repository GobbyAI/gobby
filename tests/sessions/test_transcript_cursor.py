"""Claude transcript interrupt observation and the per-CLI observer factory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.sessions.transcript_cursor import (
    ClaudeTranscriptCursor,
    TranscriptObservationError,
    build_interrupt_observer,
)

REJECTED_TOOL_RECORD = (
    json.dumps(
        {
            "type": "user",
            "toolDenialKind": "user-rejected",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "is_error": True,
                        "content": "The user doesn't want to proceed with this tool use.",
                    }
                ],
            },
        }
    ).encode()
    + b"\n"
)
INTERRUPTED_TEXT_RECORD = (
    json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "[Request interrupted by user]"}],
            },
        }
    ).encode()
    + b"\n"
)
ASSISTANT_RECORD = (
    json.dumps({"type": "assistant", "message": {"role": "assistant", "content": []}}).encode()
    + b"\n"
)
PLAIN_USER_RECORD = (
    json.dumps({"type": "user", "message": {"role": "user", "content": "hello"}}).encode() + b"\n"
)


def _append_bytes(path: Path, content: bytes) -> None:
    with path.open("ab") as stream:
        stream.write(content)


@pytest.mark.parametrize("record", [REJECTED_TOOL_RECORD, INTERRUPTED_TEXT_RECORD])
def test_claude_cursor_detects_only_fresh_interrupts(tmp_path: Path, record: bytes) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(record)
    cursor = ClaudeTranscriptCursor.at_eof(transcript)

    assert cursor.saw_fresh_interrupt() is False
    _append_bytes(transcript, ASSISTANT_RECORD + PLAIN_USER_RECORD)
    assert cursor.saw_fresh_interrupt() is False
    _append_bytes(transcript, record)
    assert cursor.saw_fresh_interrupt() is True
    assert cursor.saw_fresh_interrupt() is False


def test_claude_cursor_ignores_a_historical_partial_line(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(ASSISTANT_RECORD + REJECTED_TOOL_RECORD[:40])
    cursor = ClaudeTranscriptCursor.at_eof(transcript)

    _append_bytes(transcript, REJECTED_TOOL_RECORD[40:])
    assert cursor.saw_fresh_interrupt() is False
    _append_bytes(transcript, INTERRUPTED_TEXT_RECORD)
    assert cursor.saw_fresh_interrupt() is True


def test_claude_cursor_requires_a_readable_transcript(tmp_path: Path) -> None:
    with pytest.raises(TranscriptObservationError, match="unavailable"):
        ClaudeTranscriptCursor.at_eof(tmp_path / "missing.jsonl")
    with pytest.raises(TranscriptObservationError, match="no transcript path"):
        ClaudeTranscriptCursor.at_eof(None)


def test_observer_factory_covers_claude_and_codex_only(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(b"")

    assert build_interrupt_observer("droid", transcript, session_id="#1") is None
    assert build_interrupt_observer(None, transcript, session_id="#1") is None
    claude = build_interrupt_observer("claude", transcript, session_id="#1")
    codex = build_interrupt_observer("codex", transcript, session_id="#1")
    assert claude is not None and codex is not None

    _append_bytes(transcript, REJECTED_TOOL_RECORD)
    assert claude() is True
    assert codex() is False


def test_observer_factory_fails_closed_then_reports_lost_observation(tmp_path: Path) -> None:
    with pytest.raises(TranscriptObservationError):
        build_interrupt_observer("claude", tmp_path / "missing.jsonl", session_id="#1")

    transcript = tmp_path / "session.jsonl"
    transcript.write_bytes(ASSISTANT_RECORD)
    observer = build_interrupt_observer("claude", transcript, session_id="#1")
    assert observer is not None
    transcript.write_bytes(b"")

    assert observer() is None
