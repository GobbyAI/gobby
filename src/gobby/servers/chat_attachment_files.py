"""Filesystem helpers for stored chat attachment content."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
from pathlib import Path

from gobby.paths import (
    FilesHomeError,
    require_files_home,
    unlink_files_home_descendant,
)

logger = logging.getLogger(__name__)

_SAFE_PATH_PART_MAX_BYTES = 255


def safe_path_part(value: str, fallback: str) -> str:
    """Return a filesystem-safe single path component."""
    cleaned = value.replace("\x00", "").replace("/", "_").replace("\\", "_")
    cleaned = cleaned.lstrip(".")
    cleaned = re.sub(r"[^A-Za-z0-9.\-]", "_", cleaned)
    return _truncate_path_part_utf8(cleaned or fallback, fallback)


def _truncate_path_part_utf8(value: str, fallback: str) -> str:
    if len(value.encode("utf-8")) <= _SAFE_PATH_PART_MAX_BYTES:
        return value

    def utf8_prefix(text: str, max_bytes: int) -> str:
        if max_bytes <= 0:
            return ""
        encoded = text.encode("utf-8")[:max_bytes]
        return encoded.decode("utf-8", errors="ignore")

    fallback_name = utf8_prefix(fallback, _SAFE_PATH_PART_MAX_BYTES) or "attachment"
    suffix = Path(value).suffix
    stem = value[: -len(suffix)] if suffix else value
    if suffix:
        suffix = utf8_prefix(suffix, _SAFE_PATH_PART_MAX_BYTES - 1)
    suffix_bytes = suffix.encode("utf-8")
    stem_budget = _SAFE_PATH_PART_MAX_BYTES - len(suffix_bytes)
    stem = utf8_prefix(stem, stem_budget).rstrip("._-")
    candidate = f"{stem}{suffix}" if stem else fallback_name
    if len(candidate.encode("utf-8")) > _SAFE_PATH_PART_MAX_BYTES:
        candidate = utf8_prefix(candidate, _SAFE_PATH_PART_MAX_BYTES)
    return candidate or fallback_name


def attachment_relative_locator(project_id: str, attachment_id: str, filename: str) -> str:
    """Return the files_home-relative locator persisted on new uploads."""
    safe_project = safe_path_part(project_id, "project")
    safe_name = safe_path_part(filename, "attachment")
    return (
        f"_personal/attachments/{safe_project}/"
        f"{attachment_id[:2]}/{attachment_id}/{safe_name}"
    )


def attachment_temp_locator(project_id: str, attachment_id: str, filename: str) -> str:
    """Return the deterministic exclusive temp locator for an upload."""
    final_name = safe_path_part(filename, "attachment")
    temp_name = safe_path_part(f"{final_name}.tmp", "attachment.tmp")
    parent = str(Path(attachment_relative_locator(project_id, attachment_id, filename)).parent)
    return f"{parent}/{temp_name}"


def resolve_attachment_dir(project_id: str, attachment_id: str) -> Path:
    """Reconstruct the owner attachment directory under files_home."""
    root = require_files_home()
    return (
        root
        / "_personal"
        / "attachments"
        / safe_path_part(project_id, "project")
        / attachment_id[:2]
        / attachment_id
    )


def resolve_attachment_locator(
    project_id: str,
    attachment_id: str,
    filename: str,
    stored_local_path: str | None = None,
) -> str:
    """Reconstruct the relative locator, ignoring a stale absolute stored path."""
    del stored_local_path
    return attachment_relative_locator(project_id, attachment_id, filename)


def unlink_attachment_locator(locator: str) -> None:
    """Unlink reconstructed attachment bytes through the held files_home fd."""
    require_files_home()
    unlink_files_home_descendant(locator)


def unlink_attachment_bytes(project_id: str, attachment_id: str, filename: str) -> None:
    """Unlink reconstructed attachment bytes, treating a missing file as success."""
    locator = attachment_relative_locator(project_id, attachment_id, filename)
    temp_locator = attachment_temp_locator(project_id, attachment_id, filename)
    require_files_home()
    for candidate in (locator, temp_locator):
        try:
            unlink_files_home_descendant(candidate)
        except FileNotFoundError:
            continue


async def unlink_stored_attachment_file(
    project_id: str,
    attachment_id: str,
    filename: str,
    *,
    record_id: str,
) -> bool:
    """Best-effort unlink through the owner resolver."""
    try:
        await asyncio.to_thread(unlink_attachment_bytes, project_id, attachment_id, filename)
        return True
    except FilesHomeError:
        logger.warning(
            "Failed to delete attachment file for cleared chat %s",
            record_id,
            exc_info=True,
        )
        return False


def unlink_stale_attachment_file_sync(
    project_id: str,
    attachment_id: str,
    filename: str,
) -> bool:
    """Synchronously unlink reconstructed attachment bytes."""
    try:
        unlink_attachment_bytes(project_id, attachment_id, filename)
        return True
    except FilesHomeError:
        logger.warning(
            "Failed to remove stale chat attachment file %s",
            attachment_id,
            exc_info=True,
        )
        return False


def open_attachment_descriptor(locator: str) -> tuple[int, os.stat_result]:
    """Open attachment bytes no-follow and return the verified descriptor."""
    from gobby.paths import assert_held_files_home_identity, open_files_home_descendant

    require_files_home()
    flags = os.O_RDONLY
    fd = open_files_home_descendant(locator, flags)
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            raise FileNotFoundError("attachment locator is not a regular file")
        assert_held_files_home_identity()
    except Exception:
        os.close(fd)
        raise
    return fd, stat_result
