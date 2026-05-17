"""Attachment helpers for WebSocket terminal proxy messages."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from gobby.paths import get_gobby_home

MAX_PROXY_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_PROXY_ATTACHMENT_COUNT = 10
MAX_PROXY_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _format_attachment_size_limit(byte_count: int) -> str:
    if byte_count % (1024 * 1024) == 0:
        return f"{byte_count // (1024 * 1024)} MB"
    return f"{byte_count} bytes"


def _safe_path_part(value: str, fallback: str) -> str:
    cleaned = value.replace("\x00", "").replace("/", "_").replace("\\", "_")
    cleaned = cleaned.lstrip(".")
    cleaned = re.sub(r"[^\w.\-]", "_", cleaned)
    return cleaned or fallback


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
    if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
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


def _decode_attachment_payload(raw_name: str, raw_data: str) -> tuple[str, bytes]:
    try:
        content = base64.b64decode(raw_data, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"Attachment {raw_name!r} has invalid base64 content") from exc
    if len(content) > MAX_PROXY_ATTACHMENT_BYTES:
        raise ValueError(f"Attachment {raw_name!r} exceeds 25 MB")
    return _safe_path_part(raw_name, "attachment"), content


async def store_proxy_attachments(session_id: str, attachments: list[Any]) -> list[Path]:
    """Persist uploaded files for a tmux-delivered proxy message."""
    if not attachments:
        return []

    safe_session = _safe_path_part(session_id, "session")
    prepared_attachments = _validate_attachment_limits(attachments)
    decoded_attachments = [
        _decode_attachment_payload(raw_name, raw_data)
        for raw_name, raw_data in prepared_attachments
    ]
    if sum(len(content) for _, content in decoded_attachments) > MAX_PROXY_TOTAL_ATTACHMENT_BYTES:
        limit = _format_attachment_size_limit(MAX_PROXY_TOTAL_ATTACHMENT_BYTES)
        raise ValueError(f"Attachments exceed {limit} total")

    target_dir = get_gobby_home() / "attachments" / "attached-sessions" / safe_session
    await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)

    stored_paths: list[Path] = []
    for safe_name, content in decoded_attachments:
        target = target_dir / f"{uuid4().hex}_{safe_name}"
        await asyncio.to_thread(target.write_bytes, content)
        stored_paths.append(target)
    return stored_paths


def append_attachment_paths(content: str, paths: list[Path]) -> str:
    """Append daemon-local attachment paths to the delivered prompt text."""
    if not paths:
        return content

    attachment_text = "\n".join(str(path) for path in paths)
    if content.strip():
        return f"{content.rstrip()}\n\nAttachments:\n{attachment_text}"
    return f"Attachments:\n{attachment_text}"
