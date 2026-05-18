"""Filesystem helpers for stored chat attachment content."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)


def resolve_stored_attachment_path(local_path: str | None) -> Path | None:
    """Return a safe stored attachment path, or None for invalid metadata."""
    if not isinstance(local_path, str) or not local_path.strip():
        return None
    try:
        path = Path(local_path).expanduser().resolve()
        storage_root = (get_gobby_home() / "projects").resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if path == storage_root or storage_root not in path.parents:
        return None
    return path


async def unlink_stored_attachment_file(
    local_path: str | None,
    *,
    record_id: str,
) -> bool:
    """Best-effort unlink for a validated stored attachment path."""
    path = resolve_stored_attachment_path(local_path)
    if path is None:
        logger.warning("Skipping invalid chat attachment path for record %s", record_id)
        return False
    try:
        await asyncio.to_thread(path.unlink, missing_ok=True)
        return True
    except OSError:
        logger.warning(
            "Failed to delete attachment file for cleared chat %s",
            record_id,
            exc_info=True,
        )
        return False


def unlink_stale_attachment_file_sync(local_path: str | None) -> tuple[Path | None, bool]:
    """Synchronously unlink a stale attachment file after validating containment."""
    path = resolve_stored_attachment_path(local_path)
    if path is None:
        return None, False
    try:
        path.unlink(missing_ok=True)
        return path, True
    except OSError:
        logger.warning("Failed to remove stale chat attachment file %s", path, exc_info=True)
        return path, False
