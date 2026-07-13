"""Bounded-stream regressions for the TTS sentence buffer."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest

import gobby.voice.sentence_buffer as sentence_buffer_module
from gobby.voice.sentence_buffer import SentenceBuffer

pytestmark = pytest.mark.unit


def test_tail_scan_detects_boundary_split_across_chunks() -> None:
    buf = SentenceBuffer()

    assert buf.feed("Hello world.") == []
    assert buf.feed(" Next sentence") == ["Hello world."]
    assert buf.flush() == ["Next sentence"]


def test_boundary_free_stream_emits_before_flush_and_bounds_buffer() -> None:
    max_chunk_chars = 20
    buf = SentenceBuffer(max_chunk_chars=max_chunk_chars)
    emitted: list[str] = []
    chunks = ["abcdefghij"] * 100

    for chunk in chunks:
        emitted.extend(buf.feed(chunk))
        assert len(buf._buffer) <= max_chunk_chars * sentence_buffer_module._MAX_BUFFER_MULTIPLIER

    assert emitted
    assert "".join([*emitted, *buf.flush()]) == "".join(chunks)


def test_boundary_scan_work_is_proportional_to_new_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_pattern = sentence_buffer_module._SENTENCE_END
    scanned_chars: list[int] = []

    class TrackingPattern:
        def finditer(self, text: str, pos: int = 0) -> Iterator[re.Match[str]]:
            scanned_chars.append(len(text) - pos)
            return original_pattern.finditer(text, pos)

    monkeypatch.setattr(sentence_buffer_module, "_SENTENCE_END", TrackingPattern())
    buf = SentenceBuffer(max_chunk_chars=32)
    chunks = ["abcdefg"] * 10_000

    for chunk in chunks:
        buf.feed(chunk)

    total_input_chars = sum(map(len, chunks))
    assert max(scanned_chars) <= len(chunks[0]) + 1
    assert sum(scanned_chars) <= total_input_chars + len(chunks)
