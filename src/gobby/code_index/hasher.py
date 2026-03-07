"""Content hashing for incremental indexing."""

from __future__ import annotations

import hashlib
from pathlib import Path


def file_content_hash(path: Path | str) -> str:
    """SHA-256 hash of entire file contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def symbol_content_hash(source: bytes, start: int, end: int) -> str:
    """SHA-256 hash of a byte slice (symbol source)."""
    return hashlib.sha256(source[start:end]).hexdigest()
