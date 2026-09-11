"""Bounded retention of diagnostic hook quarantine artifacts."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gobby.hooks.envelope_dedupe import DirectoryPruneResult
from gobby.hooks.inbox import get_hook_inbox_dir, get_hook_quarantine_dir

logger = logging.getLogger(__name__)

# Quarantined envelopes are diagnostic artifacts. The window is fixed: no
# config override, measured on wall-clock time against the sidecar timestamp.
HOOK_QUARANTINE_RETENTION_WINDOW: Final = 24 * 60 * 60.0
HOOK_QUARANTINE_PRUNE_MAX_ENTRIES: Final = 100_000
_META_SUFFIX: Final = ".meta.json"


def _parse_quarantined_at(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _sidecar_quarantined_at(meta_path: Path) -> float | None:
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _parse_quarantined_at(payload.get("quarantined_at"))


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _unlink_quiet(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def _payload_name_for_quarantine_entry(name: str) -> str | None:
    if name.endswith(_META_SUFFIX):
        payload = name[: -len(_META_SUFFIX)]
        return payload if payload.endswith(".json") else None
    if name.endswith(".json"):
        return name
    return None


def _prune_quarantine_pair(
    quarantine_dir: Path,
    payload_name: str,
    *,
    cutoff: float,
) -> int:
    """Delete a payload/sidecar pair (or orphan) when strictly older than cutoff."""
    payload_path = quarantine_dir / payload_name
    meta_path = quarantine_dir / f"{payload_name}{_META_SUFFIX}"
    timestamp = _sidecar_quarantined_at(meta_path)
    if timestamp is None:
        timestamp = _file_mtime(payload_path) or _file_mtime(meta_path)
    if timestamp is None or timestamp >= cutoff:
        return 0
    deleted = 0
    if _unlink_quiet(payload_path):
        deleted += 1
    if _unlink_quiet(meta_path):
        deleted += 1
    return deleted


def prune_hook_quarantine(
    inbox_dir: Path | None = None,
    *,
    now: float | None = None,
    retention_seconds: float | None = None,
    max_entries: int = HOOK_QUARANTINE_PRUNE_MAX_ENTRIES,
) -> DirectoryPruneResult:
    """Drop quarantined envelopes past the retention window, in bounded batches.

    Eligibility is strictly after the cutoff against the sidecar timestamp.
    An entry exactly at the boundary is retained. Payload and sidecar are
    removed together; orphaned halves of a pair are recovered.
    """
    root = inbox_dir if inbox_dir is not None else get_hook_inbox_dir()
    quarantine_dir = get_hook_quarantine_dir(root)
    window = HOOK_QUARANTINE_RETENTION_WINDOW if retention_seconds is None else retention_seconds
    cutoff = (now if now is not None else time.time()) - window
    examined = 0
    deleted = 0
    truncated = False
    seen: set[str] = set()
    try:
        with os.scandir(quarantine_dir) as entries:
            for entry in entries:
                if examined >= max_entries:
                    truncated = True
                    break
                examined += 1
                payload_name = _payload_name_for_quarantine_entry(entry.name)
                if payload_name is None or payload_name in seen:
                    continue
                seen.add(payload_name)
                deleted += _prune_quarantine_pair(quarantine_dir, payload_name, cutoff=cutoff)
    except OSError:
        return DirectoryPruneResult(examined=examined, deleted=deleted)
    return DirectoryPruneResult(examined=examined, deleted=deleted, truncated=truncated)


async def hook_quarantine_retention_loop(
    is_shutdown_requested: Callable[[], bool],
    interval_seconds: int = 3600,
    inbox_dir: Path | None = None,
) -> None:
    """Periodic prune of the hook inbox quarantine directory."""
    try:
        await asyncio.to_thread(prune_hook_quarantine, inbox_dir)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Initial hook quarantine prune failed: %s", exc)

    while not is_shutdown_requested():
        try:
            await asyncio.sleep(interval_seconds)
            result = await asyncio.to_thread(prune_hook_quarantine, inbox_dir)
            if result.deleted:
                logger.info(
                    "Pruned %s expired hook quarantine file(s)",
                    result.deleted,
                    extra={
                        "event": "hook_quarantine_pruned",
                        "examined": result.examined,
                        "deleted": result.deleted,
                        "backlog_remaining": result.truncated,
                    },
                )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Hook quarantine prune loop failed: %s", exc)
