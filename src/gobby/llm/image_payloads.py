"""Bounded, non-blocking image preparation for LLM providers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import mimetypes
import os
import stat
from collections.abc import Sequence
from pathlib import Path

from gobby.llm.base import VisionInputError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REQUEST_IMAGES = 8
MAX_REQUEST_IMAGE_BYTES = 24 * 1024 * 1024
_SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})


def _image_ref(image: str) -> str:
    if image.startswith("data:") and len(image) > 80:
        return f"{image[:80]}…"
    return image


def _encode_image(mime_type: str, image_bytes: bytes) -> tuple[str, str]:
    encoded = base64.standard_b64encode(image_bytes).decode("utf-8")
    return encoded, f"data:{mime_type};base64,{encoded}"


def _prepare_data_url(image: str) -> tuple[None, str, str, str]:
    if not image.startswith("data:") or "," not in image:
        raise VisionInputError(f"Malformed data URL: {_image_ref(image)}")
    header, payload = image[5:].split(",", 1)
    tokens = [token.strip() for token in header.split(";") if token.strip()]
    if not tokens or "base64" not in {token.lower() for token in tokens[1:]}:
        raise VisionInputError(f"Malformed data URL: {_image_ref(image)}")
    mime_type = tokens[0].lower()
    if mime_type not in _SUPPORTED_IMAGE_MIME_TYPES:
        raise VisionInputError(f"Disallowed image MIME type {mime_type!r}: {_image_ref(image)}")
    compact = "".join(payload.split())
    try:
        image_bytes = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise VisionInputError(f"Invalid image base64: {_image_ref(image)}") from exc
    if not image_bytes:
        raise VisionInputError(f"Invalid image base64: {_image_ref(image)}")
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise VisionInputError(
            f"Image exceeds {MAX_IMAGE_BYTES} byte limit: {len(image_bytes)} bytes"
        )
    encoded, data_url = _encode_image(mime_type, image_bytes)
    return None, mime_type, encoded, data_url


def _prepare_image_data_sync(
    image_path: str,
    logger: logging.Logger | None,
) -> tuple[Path | None, str, str, str]:
    if image_path.startswith("data:"):
        return _prepare_data_url(image_path)

    path = Path(image_path)
    if not path.is_absolute():
        raise VisionInputError(f"Image path must be absolute: {image_path}")
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
        raise VisionInputError(f"Disallowed image MIME type {mime_type!r}: {image_path}")
    encoded, data_url = _encode_image(mime_type, image_bytes)
    return path, mime_type, encoded, data_url


async def prepare_image_data(
    image_path: str,
    logger: logging.Logger | None = None,
) -> tuple[Path | None, str, str, str]:
    """Stat, bounded-read, detect, and encode an image outside the event loop."""
    return await asyncio.to_thread(_prepare_image_data_sync, image_path, logger)


async def prepare_image_inputs(
    images: Sequence[str],
    logger: logging.Logger | None = None,
) -> list[tuple[Path | None, str, str, str]]:
    """Normalize a request image list with count and aggregate decoded-byte bounds."""
    if len(images) > MAX_REQUEST_IMAGES:
        extra = images[MAX_REQUEST_IMAGES]
        raise VisionInputError(f"Too many images (max {MAX_REQUEST_IMAGES}): {_image_ref(extra)}")
    prepared: list[tuple[Path | None, str, str, str]] = []
    total = 0
    for image in images:
        item = await prepare_image_data(image, logger)
        total += len(base64.standard_b64decode(item[2]))
        if total > MAX_REQUEST_IMAGE_BYTES:
            raise VisionInputError(
                f"Images exceed {MAX_REQUEST_IMAGE_BYTES} byte aggregate limit: {_image_ref(image)}"
            )
        prepared.append(item)
    return prepared
