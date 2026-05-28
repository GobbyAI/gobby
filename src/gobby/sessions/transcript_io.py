"""Transcript file and archive IO helpers."""

from __future__ import annotations

import functools
import gzip
import json
import logging
import zlib
from typing import Any

logger = logging.getLogger(__name__)

_ARCHIVE_CACHE_SIZE = 32


def _count_nonempty_lines(lines: list[str]) -> int:
    """Count non-empty JSONL records."""
    return sum(1 for line in lines if line.strip())


@functools.lru_cache(maxsize=_ARCHIVE_CACHE_SIZE)
def _decompress_archive(archive_path: str) -> list[str]:
    """Decompress a gzip archive and return lines."""
    lines = []
    try:
        with gzip.open(archive_path, "rt", encoding="utf-8") as f:
            for line in f:
                lines.append(line)
    except (EOFError, gzip.BadGzipFile, zlib.error) as e:
        logger.warning(f"Truncated or malformed gzip archive {archive_path}: {e}")
    return lines


def _read_jsonl_lines(path: str) -> list[str]:
    """Read lines from a JSONL file. Runs in a thread."""
    with open(path, encoding="utf-8") as f:
        return f.readlines()


def _read_json_file(path: str) -> dict[str, Any]:
    """Read and parse a JSON file. Runs in a thread."""
    with open(path, encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)
        return result


def clear_archive_cache() -> None:
    """Clear the LRU cache for decompressed archives."""
    _decompress_archive.cache_clear()
