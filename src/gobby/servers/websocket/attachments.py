"""Attachment helpers for WebSocket terminal proxy messages."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import shutil
import time
import unicodedata
from pathlib import Path
from typing import Any
from uuid import uuid4

from gobby.paths import get_gobby_home

logger = logging.getLogger(__name__)

# Terminal proxy messages allow up to 10 attachments, each up to 25 MB.
MAX_PROXY_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_PROXY_ATTACHMENT_COUNT = 10
MAX_PROXY_TOTAL_ATTACHMENT_BYTES = MAX_PROXY_ATTACHMENT_BYTES * MAX_PROXY_ATTACHMENT_COUNT
PROXY_ATTACHMENT_RETENTION_DAYS = 7
PROXY_ATTACHMENT_RETENTION_SECONDS = PROXY_ATTACHMENT_RETENTION_DAYS * 24 * 60 * 60
_SAFE_PATH_PART_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_PATH_PART_MAX_LENGTH = 120
_BASE64_WRITE_CHUNK_CHARS = 256 * 1024


def _format_attachment_size_limit(byte_count: int) -> str:
    if byte_count % (1024 * 1024) == 0:
        return f"{byte_count // (1024 * 1024)} MB"
    return f"{byte_count} bytes"


def _safe_path_part(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\x00", ""))
    cleaned = _SAFE_PATH_PART_PATTERN.sub("_", normalized)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
    cleaned = cleaned.lstrip(".").rstrip("_-")
    cleaned = _truncate_preserving_extension(cleaned, fallback)
    return cleaned or fallback


def _truncate_preserving_extension(value: str, fallback: str) -> str:
    if len(value) <= _SAFE_PATH_PART_MAX_LENGTH:
        return value
    suffix = Path(value).suffix
    budget = max(1, _SAFE_PATH_PART_MAX_LENGTH - len(suffix))
    stem = value[: -len(suffix)] if suffix else value
    stem = stem[:budget].rstrip("._-")
    return f"{stem}{suffix}" if stem else fallback


def _raw_attachment_name(item: dict[str, Any]) -> str:
    raw_name = item.get("name") or item.get("filename") or "attachment"
    if not isinstance(raw_name, str):
        raw_name = "attachment"
    return raw_name


def _raw_attachment_data(item: dict[str, Any], raw_name: str) -> str:
    raw_data = item.get("base64") or item.get("data")
    if not isinstance(raw_data, str) or not raw_data:
        raise ValueError(f"Attachment {raw_name!r} is missing base64 content")
    return _strip_data_url_prefix(raw_data)


def _strip_data_url_prefix(raw_data: str) -> str:
    if "," in raw_data and raw_data.lstrip().startswith("data:"):
        return raw_data.split(",", 1)[1]
    return raw_data


def _estimated_base64_size(raw_data: str) -> int:
    normalized = "".join(raw_data.split())
    padding = len(normalized) - len(normalized.rstrip("="))
    return max(0, (len(normalized) * 3) // 4 - padding)


def _declared_attachment_size(item: dict[str, Any], raw_name: str, raw_data: str) -> int:
    raw_size = item.get("size")
    if raw_size is None:
        return _estimated_base64_size(raw_data)
    if not isinstance(raw_size, int) or isinstance(raw_size, bool) or raw_size < 0:
        raise ValueError(f"Attachment {raw_name!r} has invalid size metadata")
    return int(raw_size)


def _validate_attachment_limits(attachments: list[Any]) -> list[tuple[str, str]]:
    if len(attachments) > MAX_PROXY_ATTACHMENT_COUNT:
        raise ValueError(f"Too many attachments: maximum is {MAX_PROXY_ATTACHMENT_COUNT}")

    prepared: list[tuple[str, str]] = []
    total_size = 0
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("Attachment entries must be objects")
        raw_name = _raw_attachment_name(item)
        raw_data = _raw_attachment_data(item, raw_name)
        size = _declared_attachment_size(item, raw_name, raw_data)
        if size > MAX_PROXY_ATTACHMENT_BYTES:
            raise ValueError(f"Attachment {raw_name!r} exceeds 25 MB")
        total_size += size
        if total_size > MAX_PROXY_TOTAL_ATTACHMENT_BYTES:
            limit = _format_attachment_size_limit(MAX_PROXY_TOTAL_ATTACHMENT_BYTES)
            raise ValueError(f"Attachments exceed {limit} total")
        prepared.append((raw_name, raw_data))
    return prepared


def _safe_attachment_name(raw_name: str) -> str:
    return _safe_path_part(raw_name, "attachment")


def _write_base64_attachment(
    target: Path,
    raw_name: str,
    raw_data: str,
    *,
    max_decoded_bytes: int = MAX_PROXY_ATTACHMENT_BYTES,
) -> int:
    written = 0
    chunk_size = _BASE64_WRITE_CHUNK_CHARS - (_BASE64_WRITE_CHUNK_CHARS % 4)
    try:
        with target.open("wb") as handle:
            for start in range(0, len(raw_data), chunk_size):
                chunk = raw_data[start : start + chunk_size]
                try:
                    decoded = base64.b64decode(chunk, validate=True)
                except binascii.Error as exc:
                    raise ValueError(f"Attachment {raw_name!r} has invalid base64 content") from exc
                projected_written = written + len(decoded)
                if projected_written > MAX_PROXY_ATTACHMENT_BYTES:
                    raise ValueError(f"Attachment {raw_name!r} exceeds 25 MB")
                if projected_written > max_decoded_bytes:
                    limit = _format_attachment_size_limit(MAX_PROXY_TOTAL_ATTACHMENT_BYTES)
                    raise ValueError(f"Attachments exceed {limit} total")
                handle.write(decoded)
                written = projected_written
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return written


def _remove_empty_attachment_dirs(target_dir: Path) -> None:
    """Best-effort prune for empty scratch dirs after a failed write."""
    root = get_gobby_home() / "attachments"
    current = target_dir
    while True:
        try:
            current.rmdir()
        except OSError:
            return
        if current == root:
            return
        current = current.parent


def _cleanup_failed_attachment_write(paths: list[Path], target_dir: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)
    _remove_empty_attachment_dirs(target_dir)


def _attached_session_dir(session_id: str) -> Path:
    safe_session = _safe_path_part(session_id, "session")
    return get_gobby_home() / "attachments" / "attached-sessions" / safe_session


async def store_proxy_attachments(session_id: str, attachments: list[Any]) -> list[Path]:
    """Persist uploaded files for a tmux-delivered proxy message.

    Files live under ``attachments/attached-sessions/{session_id}`` and are
    daemon-local scratch data. Session-expiry and cron cleanup callers should
    remove the session directory with ``cleanup_proxy_attachments_for_session``;
    stale directories may be pruned after ``PROXY_ATTACHMENT_RETENTION_SECONDS``.
    """
    if not attachments:
        return []

    prepared_attachments = _validate_attachment_limits(attachments)
    target_dir = _attached_session_dir(session_id)
    await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)

    stored_paths: list[Path] = []
    active_target: Path | None = None
    total_written = 0
    try:
        for raw_name, raw_data in prepared_attachments:
            safe_name = _safe_attachment_name(raw_name)
            active_target = target_dir / f"{uuid4().hex}_{safe_name}"
            remaining_total = MAX_PROXY_TOTAL_ATTACHMENT_BYTES - total_written
            written = await asyncio.to_thread(
                _write_base64_attachment,
                active_target,
                raw_name,
                raw_data,
                max_decoded_bytes=remaining_total,
            )
            total_written += written
            if total_written > MAX_PROXY_TOTAL_ATTACHMENT_BYTES:
                limit = _format_attachment_size_limit(MAX_PROXY_TOTAL_ATTACHMENT_BYTES)
                raise ValueError(f"Attachments exceed {limit} total")
            stored_paths.append(active_target)
            active_target = None
    except Exception:
        cleanup_paths = list(stored_paths)
        if active_target is not None:
            cleanup_paths.append(active_target)
        await asyncio.to_thread(_cleanup_failed_attachment_write, cleanup_paths, target_dir)
        raise
    return stored_paths


async def cleanup_proxy_attachments_for_session(session_id: str) -> int:
    """Delete stored proxy attachments for a completed or expired attached session."""
    target_dir = _attached_session_dir(session_id)

    def remove_tree() -> int:
        if not target_dir.exists():
            return 0
        removed = sum(1 for path in target_dir.rglob("*") if path.is_file())
        try:
            shutil.rmtree(target_dir)
        except FileNotFoundError:
            return 0
        except OSError:
            logger.warning(
                "Failed to remove proxy attachment directory %s",
                target_dir,
                exc_info=True,
            )
            return 0
        return removed

    return await asyncio.to_thread(remove_tree)


async def cleanup_expired_proxy_attachments(
    *,
    retention_seconds: int = PROXY_ATTACHMENT_RETENTION_SECONDS,
) -> int:
    """Delete attached-session proxy attachment directories older than retention."""
    root = get_gobby_home() / "attachments" / "attached-sessions"
    cutoff = time.time() - retention_seconds

    def remove_expired() -> int:
        if not root.exists():
            return 0
        removed = 0
        for session_dir in root.iterdir():
            if not session_dir.is_dir():
                continue
            try:
                newest_mtime = max(
                    [session_dir.stat().st_mtime]
                    + [path.stat().st_mtime for path in session_dir.rglob("*")]
                )
            except FileNotFoundError:
                continue
            except Exception:
                logger.warning(
                    "Failed to inspect proxy attachment directory %s",
                    session_dir,
                    exc_info=True,
                )
                continue
            if newest_mtime > cutoff:
                continue
            file_count = sum(1 for path in session_dir.rglob("*") if path.is_file())
            try:
                shutil.rmtree(session_dir)
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning(
                    "Failed to remove expired proxy attachment directory %s",
                    session_dir,
                    exc_info=True,
                )
                continue
            removed += file_count
            logger.info(
                "Removed %s expired proxy attachment file(s) from %s",
                file_count,
                session_dir,
            )
        return removed

    return await asyncio.to_thread(remove_expired)


def append_attachment_paths(content: str, paths: list[Path]) -> str:
    """Append daemon-local attachment paths to the delivered prompt text."""
    if not paths:
        return content

    attachment_text = "\n".join(str(path) for path in paths)
    if content.strip():
        return f"{content.rstrip()}\n\nAttachments:\n{attachment_text}"
    return f"Attachments:\n{attachment_text}"
