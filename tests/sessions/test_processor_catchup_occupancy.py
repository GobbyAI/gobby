"""Transcript catch-up keeps the context-pressure guard on live occupancy (#22884).

A daemon restart while a Claude session kept writing used to reject the index
sidecar, reparse the whole transcript in one pass under the session's processing
lock, and leave ``context_used_tokens`` at its pre-restart value until that pass
finished (15 minutes for a 122 MB transcript).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.sessions import processor_transcripts, processor_usage
from gobby.sessions.processor import SessionMessageProcessor
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.workflows.observer_context_usage import (
    BLOCK_MESSAGE_VARIABLE,
    PRESSURE_BAND_VARIABLE,
    detect_context_compact_guidance,
)

pytestmark = pytest.mark.unit

SESSION_ID = "session-catchup"
MESSAGE_COUNT = 20
FIRST_USED = 100_000
USED_STEP = 1_000
LAST_USED = FIRST_USED + (MESSAGE_COUNT - 1) * USED_STEP
CHUNK_BYTES = 4096


def _assistant_line(index: int, first_used: int = FIRST_USED) -> str:
    # input + cache_read + cache_creation is the reported occupancy.
    used = first_used + index * USED_STEP
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

        self.session_manager = MagicMock()
        self.session_manager.get.return_value = self.session
        self.session_manager.update_context_usage.side_effect = persist_context
        self.processor = SessionMessageProcessor(MagicMock(), session_manager=self.session_manager)

    def restart(self) -> None:
        """Replace the processor as a daemon restart does; the session row persists."""
        self.processor = SessionMessageProcessor(MagicMock(), session_manager=self.session_manager)


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


def _oversized_tool_result_line() -> str:
    # A tool result larger than the tail window, carrying no usage of its own.
    return json.dumps(
        {
            "type": "user",
            "uuid": "uuid-oversized",
            "timestamp": "2026-09-24T19:13:00Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-oversized",
                        "content": "y" * (processor_usage.TAIL_OCCUPANCY_BYTES + 64 * 1024),
                    }
                ],
            },
        }
    )


def _append(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(f"{line}\n" for line in lines))


async def test_tail_occupancy_reads_past_oversized_trailing_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processor_transcripts, "CATCHUP_CHUNK_BYTES", CHUNK_BYTES)
    transcript = tmp_path / "claude.jsonl"
    _write_transcript(transcript)
    _append(transcript, [_oversized_tool_result_line()])
    harness = _Harness(monkeypatch)
    harness.processor.register_session(SESSION_ID, str(transcript))

    caught_up = await harness.processor._process_session(SESSION_ID, str(transcript))

    assert caught_up is False
    assert harness.published == [LAST_USED]


class _GuardSessions:
    """The context guard's view of the session row the processor writes."""

    def __init__(self, harness: _Harness) -> None:
        self._harness = harness

    def get(self, _session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            context_used_tokens=self._harness.session.context_used_tokens,
            context_window=self._harness.session.context_window,
        )


def _guard_band(variables: dict[str, Any], harness: _Harness) -> str:
    variables["parent_turn_seq"] = variables.get("parent_turn_seq", -1) + 1
    detect_context_compact_guidance(variables, SESSION_ID, _GuardSessions(harness))
    return str(variables[PRESSURE_BAND_VARIABLE])


async def _bands_through_catchup(
    harness: _Harness, transcript: Path, variables: dict[str, Any]
) -> list[str]:
    bands = []
    while True:
        caught_up = await harness.processor._process_session(SESSION_ID, str(transcript))
        bands.append(_guard_band(variables, harness))
        if caught_up:
            return bands


async def test_context_guard_warns_then_blocks_across_restart_catchup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#22884: a 1M-window session warns at 250k and blocks at 300k through catch-up."""
    monkeypatch.setattr(processor_transcripts, "CATCHUP_CHUNK_BYTES", CHUNK_BYTES)
    transcript = tmp_path / "claude.jsonl"
    # History climbs from 231k; the latest record reports exactly 250k.
    transcript.write_text(
        "".join(f"{_assistant_line(i, first_used=231_000)}\n" for i in range(MESSAGE_COUNT)),
        encoding="utf-8",
    )
    _append(transcript, [_oversized_tool_result_line()])
    harness = _Harness(monkeypatch)
    harness.processor.register_session(SESSION_ID, str(transcript))
    variables: dict[str, Any] = {"chat_mode": "normal"}

    warm_bands = await _bands_through_catchup(harness, transcript, variables)

    assert len(warm_bands) > 1
    assert set(warm_bands) == {"warn"}

    # The daemon restarts while the session keeps writing up to 300k.
    harness.restart()
    _append(
        transcript,
        [_assistant_line(i, first_used=261_000) for i in range(MESSAGE_COUNT, 2 * MESSAGE_COUNT)]
        + [_oversized_tool_result_line()],
    )
    harness.processor.register_session(SESSION_ID, str(transcript))
    assert _guard_band(variables, harness) == "warn"

    restart_bands = await _bands_through_catchup(harness, transcript, variables)

    assert len(restart_bands) > 1
    assert set(restart_bands) == {"block"}
    assert harness.session.context_used_tokens == 300_000
    assert variables[BLOCK_MESSAGE_VARIABLE]


@pytest.mark.parametrize("source", ["droid", "agy"])
async def test_tail_occupancy_skips_sources_without_line_occupancy(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening must not rescan a whole transcript that can never yield occupancy."""
    reads: list[int] = []
    real_read = processor_usage._read_complete_tail_lines

    def counting_read(path: str, limit: int) -> tuple[list[str], bool]:
        reads.append(limit)
        return real_read(path, limit)

    monkeypatch.setattr(processor_usage, "_read_complete_tail_lines", counting_read)
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript)
    _append(transcript, [_oversized_tool_result_line()])
    harness = _Harness(monkeypatch)
    harness.session.source = source
    harness.processor.register_session(SESSION_ID, str(transcript), source=source)

    await harness.processor._publish_tail_occupancy(SESSION_ID, str(transcript))

    assert reads == []
    assert harness.published == []
