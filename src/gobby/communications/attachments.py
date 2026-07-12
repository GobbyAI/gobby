"""Attachment manager for communications file handling."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Platform file size limits in bytes
PLATFORM_SIZE_LIMITS: dict[str, int] = {
    "telegram": 50 * 1024 * 1024,  # 50 MB
    "slack": 1 * 1024 * 1024 * 1024,  # 1 GB (paid)
    "discord": 25 * 1024 * 1024,  # 25 MB (Nitro: 100 MB)
    "email": 25 * 1024 * 1024,  # 25 MB typical
    "sms": 5 * 1024 * 1024,  # 5 MB MMS limit
    "teams": 250 * 1024 * 1024,  # 250 MB
}


class AttachmentManager:
    """Manages downloading, storing, and cleaning up communication attachments."""

    def __init__(self, storage_dir: Path | None = None) -> None:
        self._storage_dir = storage_dir or Path.home() / ".gobby" / "comms_attachments"
        self._storage_dir.mkdir(parents=True, exist_ok=True)

    @property
    def storage_dir(self) -> Path:
        """Return the storage directory path."""
        return self._storage_dir

    async def download(
        self, url: str, filename: str, headers: dict[str, str] | None = None
    ) -> Path:
        """Download a file from a URL to local storage."""
        safe_filename = self._safe_filename(filename)
        dest = self._storage_dir / safe_filename

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, headers=headers or {}, follow_redirects=True)
            response.raise_for_status()
            await asyncio.to_thread(dest.write_bytes, response.content)

        size = (await asyncio.to_thread(dest.stat)).st_size
        logger.info(f"Downloaded attachment {safe_filename} ({size} bytes)")
        return dest

    async def store(self, content: bytes, filename: str) -> Path:
        """Store raw bytes as a file."""
        safe_filename = self._safe_filename(filename)
        dest = self._storage_dir / safe_filename
        await asyncio.to_thread(dest.write_bytes, content)
        logger.info(f"Stored attachment {safe_filename} ({len(content)} bytes)")
        return dest

    def get_path(self, filename: str) -> Path | None:
        """Look up a stored file by original filename (ignoring timestamp prefix)."""
        basename = Path(filename).name
        exact = self._storage_dir / basename
        if exact.is_file():
            return exact

        candidates = [
            path
            for path in self._storage_dir.iterdir()
            if path.is_file() and self._original_filename(path.name) == basename
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def cleanup_old(self, days: int = 30, *, limit: int = 500) -> int:
        """Remove attachments older than the retention period."""
        cutoff = time.time() - (days * 86400)
        removed = 0
        for path in self._storage_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
                if removed >= limit:
                    break
        if removed:
            logger.info(f"Cleaned up {removed} attachments older than {days} days")
        return removed

    def validate_size(self, size_bytes: int, channel_type: str) -> bool:
        """Check if a file is within the platform's size limit."""
        limit = PLATFORM_SIZE_LIMITS.get(channel_type, 25 * 1024 * 1024)
        return size_bytes <= limit

    def get_size_limit(self, channel_type: str) -> int:
        """Get the file size limit for a platform."""
        return PLATFORM_SIZE_LIMITS.get(channel_type, 25 * 1024 * 1024)

    def _safe_filename(self, filename: str) -> str:
        """Sanitize a filename, prepending a timestamp to avoid collisions."""
        basename = Path(filename).name
        # Strip null bytes and path traversal sequences
        basename = basename.replace("\x00", "").replace("/", "").replace("\\", "")
        basename = basename.lstrip(".")  # Remove leading dots (hidden files / traversal)
        # Replace unsafe characters with underscores
        basename = re.sub(r"[^\w.\-]", "_", basename)
        if not basename:
            basename = "attachment"
        return f"{int(time.time() * 1000)}_{basename}"

    def _original_filename(self, stored_name: str) -> str | None:
        prefix, separator, original = stored_name.partition("_")
        if separator and prefix.isdigit() and original:
            return original
        return None
