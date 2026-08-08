"""Shared synchronization contract for the managed Puppeteer browser cache."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from gobby.sync.jsonl_io import atomic_write_text, export_file_lock

_READINESS_FILE = ".gobby-browser-ready.json"


@dataclass(frozen=True)
class BrowserCacheReadiness:
    """Identity of browser artifacts compatible with one Puppeteer runtime."""

    platform: str
    puppeteer_version: str
    browser_build: str
    channel: str


@contextmanager
def browser_cache_lock(cache_root: Path) -> Iterator[None]:
    """Serialize producers that mutate one Puppeteer cache."""
    with export_file_lock(cache_root / ".gobby-browser-cache"):
        yield


def read_browser_cache_readiness(cache_root: Path) -> BrowserCacheReadiness | None:
    """Read a valid compatibility record, treating corruption as a cache miss."""
    try:
        value = json.loads((cache_root / _READINESS_FILE).read_text(encoding="utf-8"))
        fields = (
            value["platform"],
            value["puppeteer_version"],
            value["browser_build"],
            value["channel"],
        )
        if not all(isinstance(field, str) and field for field in fields):
            return None
        return BrowserCacheReadiness(*fields)
    except (KeyError, OSError, TypeError, ValueError):
        return None


def browser_cache_is_ready(cache_root: Path, expected: BrowserCacheReadiness) -> bool:
    """Return whether the durable record matches the producer's exact needs."""
    return read_browser_cache_readiness(cache_root) == expected


def write_browser_cache_readiness(
    cache_root: Path,
    readiness: BrowserCacheReadiness,
) -> None:
    """Publish a compatibility record atomically."""
    cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_text(
        cache_root / _READINESS_FILE,
        json.dumps(asdict(readiness), sort_keys=True) + "\n",
    )
    directory_fd = os.open(cache_root, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
