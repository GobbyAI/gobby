"""Content hashes that show a checkout path still holds the bytes a Gobby action left."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path


def file_fingerprint(path: Path) -> str | None:
    """Return git's blob hash of the path's content, or None when the path is absent.

    Raises OSError for anything else, such as a directory, so a caller can refuse to vouch for it.
    """
    try:
        data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def path_fingerprints(root: Path, paths: Iterable[str]) -> dict[str, str | None]:
    """Return each path's fingerprint under ``root``, leaving out paths that are not files."""
    fingerprints: dict[str, str | None] = {}
    for path in paths:
        try:
            fingerprints[path] = file_fingerprint(root / path)
        except OSError:
            continue
    return fingerprints
