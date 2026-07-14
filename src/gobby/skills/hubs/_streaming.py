"""Bounded HTTP response readers for skill hub providers."""

from __future__ import annotations

import httpx

from gobby.skills.limits import HUB_STREAM_CHUNK_BYTES


class SkillContentError(RuntimeError):
    """Raised when streamed skill content violates an ingestion boundary."""


async def read_limited_utf8(
    response: httpx.Response,
    *,
    max_bytes: int,
    label: str,
) -> str:
    """Read a UTF-8 response without buffering more than ``max_bytes``."""
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError as e:
            raise SkillContentError(f"Invalid {label.lower()} content length") from e
        if declared_size > max_bytes:
            raise SkillContentError(f"{label} exceeds size limit")

    content = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=HUB_STREAM_CHUNK_BYTES):
        if len(chunk) > max_bytes - len(content):
            raise SkillContentError(f"{label} exceeds size limit")
        content.extend(chunk)

    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SkillContentError(f"{label} is not valid UTF-8") from e
