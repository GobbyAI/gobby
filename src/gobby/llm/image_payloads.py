"""Bounded, non-blocking image preparation for LLM providers."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import os
import stat
from pathlib import Path

from gobby.llm.base import VisionInputError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def _prepare_image_data_sync(
    image_path: str,
    logger: logging.Logger | None,
) -> tuple[Path, str, str, str]:
    path = Path(image_path)
    try:
        path_stat = path.stat()
    except FileNotFoundError as exc:
        raise VisionInputError(f"Image not found: {image_path}") from exc
    except OSError as exc:
        raise VisionInputError(f"Failed to stat image: {exc}") from exc

    if not stat.S_ISREG(path_stat.st_mode):
        raise VisionInputError(f"Image is not a regular file: {image_path}")
    if path_stat.st_size > MAX_IMAGE_BYTES:
        raise VisionInputError(
            f"Image exceeds {MAX_IMAGE_BYTES} byte limit: {path_stat.st_size} bytes"
        )

    try:
        with path.open("rb") as image_file:
            opened_stat = os.fstat(image_file.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise VisionInputError(f"Image is not a regular file: {image_path}")
            if opened_stat.st_size > MAX_IMAGE_BYTES:
                raise VisionInputError(
                    f"Image exceeds {MAX_IMAGE_BYTES} byte limit: {opened_stat.st_size} bytes"
                )
            image_bytes = image_file.read(MAX_IMAGE_BYTES + 1)
    except VisionInputError:
        raise
    except OSError as exc:
        if logger is not None:
            logger.error("Failed to read image %s: %s", image_path, exc)
        raise VisionInputError(f"Failed to read image: {exc}") from exc

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise VisionInputError(f"Image grew beyond {MAX_IMAGE_BYTES} byte limit while reading")

    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type not in _SUPPORTED_IMAGE_MIME_TYPES:
        mime_type = "image/png"
    encoded = base64.standard_b64encode(image_bytes).decode("utf-8")
    return path, mime_type, encoded, f"data:{mime_type};base64,{encoded}"


async def prepare_image_data(
    image_path: str,
    logger: logging.Logger | None = None,
) -> tuple[Path, str, str, str]:
    """Stat, bounded-read, detect, and encode an image outside the event loop."""
    return await asyncio.to_thread(_prepare_image_data_sync, image_path, logger)
