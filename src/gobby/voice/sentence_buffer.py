"""Sentence buffer for streaming TTS.

Accumulates streaming text chunks and prefers complete sentences for TTS
synthesis while bounding boundary-free streams such as logs and code blocks.
"""

from __future__ import annotations

import re

# Sentence-ending punctuation followed by whitespace or end of string.
# Handles: "Hello world. Next sentence", "Really?! Yes.", etc.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Minimum sentence length to emit — avoids tiny fragments like "Dr." or "e.g."
# Keep low to preserve natural prosody for short exclamations ("Wow!", "Really?")
_MIN_SENTENCE_LEN = 4

# Clause-ending punctuation followed by whitespace or end of string.
_CLAUSE_END = re.compile(r"([,;:\u2014\u2013])(?:\s+|$)")

_DEFAULT_MAX_CHUNK_CHARS = 180
_MAX_BUFFER_MULTIPLIER = 4


class SentenceBuffer:
    """Accumulates streaming text and yields bounded speech chunks for TTS."""

    def __init__(
        self,
        min_length: int = _MIN_SENTENCE_LEN,
        max_chunk_chars: int = _DEFAULT_MAX_CHUNK_CHARS,
    ) -> None:
        self._buffer = ""
        self._min_length = min_length
        self._max_chunk_chars = max_chunk_chars

    def feed(self, chunk: str) -> list[str]:
        """Feed text and return complete sentences or a forced bounded prefix.

        Args:
            chunk: Partial text from the LLM stream.

        Returns:
            Speech chunks, or an empty list while the pending buffer remains bounded.
        """
        if not chunk:
            return []

        previous_length = len(self._buffer)
        self._buffer += chunk

        # The existing buffer was fully scanned by earlier feed calls. Include
        # its last character only so punctuation at the old/new boundary can
        # pair with whitespace at the start of this chunk.
        scan_start = max(0, previous_length - 1)
        sentences: list[str] = []
        remainder_start = 0
        found_boundary = False
        for match in _SENTENCE_END.finditer(self._buffer, scan_start):
            found_boundary = True
            part = self._buffer[remainder_start : match.start()]
            stripped = part.strip()
            if stripped:
                sentences.append(stripped)
            remainder_start = match.end()

        if not found_boundary:
            return self._force_emit_prefix()

        self._buffer = self._buffer[remainder_start:]

        # If any sentence is too short, merge it with the next one
        merged: list[str] = []
        carry = ""
        for s in sentences:
            combined = f"{carry} {s}".strip() if carry else s
            if len(combined) < self._min_length:
                carry = combined
            else:
                merged.append(combined)
                carry = ""

        if carry:
            # Short leftover — push back to buffer
            self._buffer = f"{carry} {self._buffer}".strip() if self._buffer else carry

        return [*self._split_chunks(merged), *self._force_emit_prefix()]

    def flush(self) -> list[str]:
        """Flush remaining buffer content (call at end of stream).

        Returns:
            Remaining text split into TTS chunks, or an empty list if buffer is empty.
        """
        text = self._buffer.strip()
        self._buffer = ""
        if not text:
            return []
        return self._split_text(text)

    def clear(self) -> None:
        """Discard all buffered text (call on cancellation)."""
        self._buffer = ""

    def _split_chunks(self, chunks: list[str]) -> list[str]:
        split: list[str] = []
        for chunk in chunks:
            split.extend(self._split_text(chunk))
        return split

    def _force_emit_prefix(self) -> list[str]:
        """Emit the oldest text when a boundary-free stream exceeds its ceiling."""
        max_buffer_chars = self._max_chunk_chars * _MAX_BUFFER_MULTIPLIER
        if len(self._buffer) <= max_buffer_chars:
            return []

        prefix_chars = max(
            self._max_chunk_chars,
            len(self._buffer) - max_buffer_chars,
        )
        search_end = min(len(self._buffer), prefix_chars + self._max_chunk_chars)
        split_at = next(
            (
                index + 1
                for index in range(prefix_chars, search_end)
                if self._buffer[index].isspace()
            ),
            prefix_chars,
        )
        prefix = self._buffer[:split_at]
        self._buffer = self._buffer[split_at:].lstrip()
        return self._split_text(prefix)

    def _split_text(self, text: str) -> list[str]:
        stripped = text.strip()
        if not stripped:
            return []
        if len(stripped) <= self._max_chunk_chars:
            return [stripped]

        pieces: list[str] = []
        current = ""
        for clause in self._split_clauses(stripped):
            if len(clause) > self._max_chunk_chars:
                if current:
                    pieces.append(current)
                    current = ""
                pieces.extend(self._split_on_whitespace(clause))
                continue

            candidate = f"{current} {clause}".strip() if current else clause
            if len(candidate) <= self._max_chunk_chars:
                current = candidate
            else:
                if current:
                    pieces.append(current)
                current = clause

        if current:
            pieces.append(current)

        return pieces

    def _split_clauses(self, text: str) -> list[str]:
        clauses: list[str] = []
        start = 0
        for match in _CLAUSE_END.finditer(text):
            clause = text[start : match.end(1)].strip()
            if clause:
                clauses.append(clause)
            start = match.end()

        remainder = text[start:].strip()
        if remainder:
            clauses.append(remainder)

        return clauses or [text]

    def _split_on_whitespace(self, text: str) -> list[str]:
        pieces: list[str] = []
        current = ""
        for word in text.split():
            candidate = f"{current} {word}".strip() if current else word
            if len(candidate) <= self._max_chunk_chars:
                current = candidate
                continue

            if current:
                pieces.append(current)
                current = ""

            if len(word) > self._max_chunk_chars:
                pieces.append(word)
            else:
                current = word

        if current:
            pieces.append(current)

        return pieces
