"""Transcript file and archive IO helpers."""

from __future__ import annotations

import gzip
import json
import logging
import zlib
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)


class DecompressionError(RuntimeError):
    """Raised when a transcript archive cannot be decompressed."""


def _count_nonempty_lines(lines: Iterable[str]) -> int:
    """Count non-empty JSONL records."""
    return sum(1 for line in lines if line.strip())


def _decompress_archive(archive_path: str) -> tuple[str, ...]:
    """Decompress a gzip archive and return lines."""
    lines: list[str] = []
    try:
        with gzip.open(archive_path, "rt", encoding="utf-8") as f:
            for line in f:
                lines.append(line)
    except (EOFError, gzip.BadGzipFile, zlib.error) as e:
        raise DecompressionError(f"Truncated or malformed gzip archive {archive_path}: {e}") from e
    return tuple(lines)


def _read_jsonl_lines(path: str) -> list[str]:
    """Read lines from a JSONL file."""
    with open(path, encoding="utf-8") as f:
        return f.readlines()


def _read_json_file(path: str) -> dict[str, Any]:
    """Read and parse a JSON file."""
    with open(path, encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)
        return result


def clear_archive_cache() -> None:
    """Backward-compatible no-op; archives are no longer retained in memory."""
