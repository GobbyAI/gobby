"""Transcript catch-up keeps the context-pressure guard on live occupancy (#22884).

A daemon restart while a Claude session kept writing used to reject the index
sidecar, reparse the whole transcript in one pass under the session's processing
lock, and leave ``context_used_tokens`` at its pre-restart value until that pass
finished (15 minutes for a 122 MB transcript).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.sessions import processor_transcripts
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

pytestmark = pytest.mark.unit

SESSION_ID = "session-catchup"
MESSAGE_COUNT = 20
FIRST_USED = 100_000
USED_STEP = 1_000
LAST_USED = FIRST_USED + (MESSAGE_COUNT - 1) * USED_STEP
CHUNK_BYTES = 4096


def _assistant_line(index: int) -> str:
    # input + cache_read + cache_creation is the reported occupancy.
    used = FIRST_USED + index * USED_STEP
    return json.dumps(
        {
            "type": "assistant",
            "uuid": f"uuid-{index}",
            "timestamp": "2026-09-24T19:12:00Z",
            "message": {
                "id": f"msg_{index}",
                "model": "claude-fable-5-1",
                "content": [{"type": "text", "text": f"step {index} " + "x" * 900}],
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": used - 15,
                    "cache_creation_input_tokens": 5,
                    "output_tokens": 50,
                },
            },
        }
    )


def _write_transcript(path: Path, count: int = MESSAGE_COUNT) -> None:
    path.write_text("".join(f"{_assistant_line(i)}\n" for i in range(count)), encoding="utf-8")


class _Harness:
    """A processor over a recording session manager with a stub token store."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        store = MagicMock()
        store.get_session_totals.return_value = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        store.record.return_value = True
        monkeypatch.setattr("gobby.sessions.processor.TokenEventStore", lambda _db: store)

        self.session = MagicMock()
        self.session.project_id = "proj-1"
        self.session.source = "claude"
        self.session.context_window = 1_000_000
        self.session.model = "claude-fable-5-1"
        self.session.context_used_tokens = None
        self.session.context_usage_confidence = None
        self.published: list[int | None] = []

        def persist_context(_session_id: str, snapshot: Any) -> bool:
            self.published.append(snapshot.context_used_tokens)
            self.session.context_used_tokens = snapshot.context_used_tokens
            return True

        session_manager = MagicMock()
        session_manager.get.return_value = self.session
        session_manager.update_context_usage.side_effect = persist_context
        self.processor = SessionMessageProcessor(MagicMock(), session_manager=session_manager)


async def _offset_after_restart_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Process a transcript, append while "down", and return a fresh processor's offset."""
    transcript = tmp_path / "claude.jsonl"
    _write_transcript(transcript, count=3)
    before = _Harness(monkeypatch)
    before.processor.register_session(SESSION_ID, str(transcript))
    assert (await before.processor.flush_session(SESSION_ID)).flushed
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(f"{_assistant_line(3)}\n")

    after = _Harness(monkeypatch)
    after.processor.register_session(SESSION_ID, str(transcript))
    return after.processor._byte_offsets.get(SESSION_ID, 0)


@pytest.mark.asyncio
async def test_claude_sidecar_resumes_after_restart_while_transcript_grew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processed_size = sum(len(_assistant_line(i)) + 1 for i in range(3))

    assert await _offset_after_restart_append(tmp_path, monkeypatch) == processed_size


@pytest.mark.asyncio
async def test_sidecar_without_incremental_state_is_rejected_after_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins the pre-fix failure: a rejected sidecar restarts ingest from byte 0."""
    monkeypatch.setattr(ClaudeTranscriptParser, "supports_incremental_state", False)

    assert await _offset_after_restart_append(tmp_path, monkeypatch) == 0


@pytest.mark.asyncio
async def test_missing_sidecar_catchup_publishes_tail_occupancy_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processor_transcripts, "CATCHUP_CHUNK_BYTES", CHUNK_BYTES)
    transcript = tmp_path / "claude.jsonl"
    _write_transcript(transcript)
    harness = _Harness(monkeypatch)
    harness.processor.register_session(SESSION_ID, str(transcript))

    caught_up = await harness.processor._process_session(SESSION_ID, str(transcript))

    assert caught_up is False
    # Only the tail's live value is published; the history chunk's is withheld.
    assert harness.published == [LAST_USED]
    line_size = len(_assistant_line(0)) + 1
    offset = harness.processor._byte_offsets[SESSION_ID]
    assert CHUNK_BYTES <= offset < CHUNK_BYTES + line_size
    assert offset < transcript.stat().st_size


@pytest.mark.asyncio
async def test_bounded_catchup_passes_reach_eof_without_regressing_occupancy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processor_transcripts, "CATCHUP_CHUNK_BYTES", CHUNK_BYTES)
    transcript = tmp_path / "claude.jsonl"
    _write_transcript(transcript)
    harness = _Harness(monkeypatch)
    harness.processor.register_session(SESSION_ID, str(transcript))

    passes = 1
    while not await harness.processor._process_session(SESSION_ID, str(transcript)):
        passes += 1

    assert passes > 1
    assert set(harness.published) == {LAST_USED}
    assert harness.session.context_used_tokens == LAST_USED
    assert harness.processor._byte_offsets[SESSION_ID] == transcript.stat().st_size
    assert harness.processor._stats[SESSION_ID]["message_count"] == MESSAGE_COUNT


@pytest.mark.asyncio
async def test_flush_session_runs_bounded_passes_to_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processor_transcripts, "CATCHUP_CHUNK_BYTES", CHUNK_BYTES)
    transcript = tmp_path / "claude.jsonl"
    _write_transcript(transcript)
    harness = _Harness(monkeypatch)
    harness.processor.register_session(SESSION_ID, str(transcript))

    result = await harness.processor.flush_session(SESSION_ID)

    assert result.flushed
    assert harness.processor._byte_offsets[SESSION_ID] == transcript.stat().st_size
    assert harness.session.context_used_tokens == LAST_USED
