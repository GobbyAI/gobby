"""Shared safeguards for daemon HTTP uploads."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import HTTPException, UploadFile

UPLOAD_CHUNK_BYTES = 1024 * 1024


async def read_bounded_upload(
    file: UploadFile,
    *,
    max_bytes: int,
    label: str,
) -> bytes:
    """Read an upload in bounded chunks, rejecting the first byte past the limit."""
    chunks: list[bytes] = []
    size = 0
    while True:
        remaining = max_bytes - size
        read_size = min(UPLOAD_CHUNK_BYTES, max(remaining + 1, 1))
        chunk = await file.read(read_size)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{label} exceeds {max_bytes} byte limit",
            )
        chunks.append(chunk)


def ensure_disk_space(directory: Path, incoming_bytes: int, *, label: str) -> None:
    """Reject an upload when its staging filesystem lacks the reserved capacity."""
    try:
        usage = shutil.disk_usage(directory)
    except OSError as exc:
        raise HTTPException(status_code=507, detail=f"{label} storage unavailable") from exc
    if usage.free < incoming_bytes:
        raise HTTPException(
            status_code=507,
            detail=f"Insufficient disk space for {label.lower()}",
        )
