"""Transcript file and archive IO helpers."""

from __future__ import annotations

import gzip
import json
import logging
import zlib
from collections.abc import Iterable, Iterator
from typing import Any

logger = logging.getLogger(__name__)


class DecompressionError(RuntimeError):
    """Raised when a transcript archive cannot be decompressed."""


class TranscriptTooLargeError(RuntimeError):
    """Raised when a non-windowable transcript exceeds the in-memory size guard.

    Native-JSON transcripts (Gemini/Qwen export style) have no line offsets, so
    they cannot be windowed and must be rendered whole. Above the size guard the
    serving layer refuses to materialize them and the route maps this to HTTP 413
    (download instead) rather than risking an OOM / opaque 500.
    """

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"Transcript too large to render ({size_bytes} bytes exceeds "
            f"{max_bytes} byte limit)"
        )


def _count_nonempty_lines(lines: Iterable[str]) -> int:
    """Count non-empty JSONL records."""
    return sum(1 for line in lines if line.strip())


def _iter_archive_lines(archive_path: str) -> Iterator[str]:
    """Stream decompressed gzip lines lazily (no whole-archive buffer).

    The ``DecompressionError`` is raised during iteration, so consumers must run
    this inside a worker thread (e.g. via ``asyncio.to_thread``) — both the
    streaming legacy read and the eager :func:`_read_archive_lines` do.
    """
    try:
        with gzip.open(archive_path, "rt", encoding="utf-8") as f:
            yield from f
    except (EOFError, gzip.BadGzipFile, zlib.error) as e:
        raise DecompressionError(f"Truncated or malformed gzip archive {archive_path}: {e}") from e


def _read_archive_lines(archive_path: str) -> list[str]:
    """Materialize all decompressed archive lines. Run off the event loop."""
    return list(_iter_archive_lines(archive_path))


def _iter_jsonl_lines(path: str) -> Iterator[str]:
    """Stream raw JSONL lines lazily for limit-capped serving reads."""
    with open(path, encoding="utf-8") as f:
        yield from f


def _read_json_file(path: str) -> dict[str, Any]:
    """Read and parse a JSON file."""
    with open(path, encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)
        return result


def clear_archive_cache() -> None:
    """Backward-compatible no-op; archives are no longer retained in memory."""
