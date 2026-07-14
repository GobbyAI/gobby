"""Shared checksum parsing helpers for native binary downloads."""

from __future__ import annotations


def parse_sha256_digest(text: str) -> str | None:
    """Extract a lowercase SHA-256 hex digest from checksum-file text.

    Accepts both a bare digest and the ``sha256sum`` line format
    (``<digest>  <filename>``). Returns ``None`` when no valid 64-character
    hex digest is present.
    """
    for line in text.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        candidate = parts[0].lower()
        if len(candidate) == 64 and all(c in "0123456789abcdef" for c in candidate):
            return candidate
    return None
