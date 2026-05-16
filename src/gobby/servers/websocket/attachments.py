"""Attachment helpers for WebSocket terminal proxy messages."""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from gobby.paths import get_gobby_home

MAX_PROXY_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _safe_path_part(value: str, fallback: str) -> str:
    cleaned = value.replace("\x00", "").replace("/", "_").replace("\\", "_")
    cleaned = cleaned.lstrip(".")
    cleaned = re.sub(r"[^\w.\-]", "_", cleaned)
    return cleaned or fallback


def _decode_attachment_payload(item: dict[str, Any]) -> tuple[str, bytes]:
    raw_name = item.get("name") or item.get("filename") or "attachment"
    if not isinstance(raw_name, str):
        raw_name = "attachment"
    raw_data = item.get("base64") or item.get("data")
    if not isinstance(raw_data, str) or not raw_data:
        raise ValueError(f"Attachment {raw_name!r} is missing base64 content")

    if "," in raw_data and raw_data.lstrip().startswith("data:"):
        raw_data = raw_data.split(",", 1)[1]
    try:
        content = base64.b64decode(raw_data, validate=True)
    except Exception as exc:
        raise ValueError(f"Attachment {raw_name!r} has invalid base64 content") from exc
    if len(content) > MAX_PROXY_ATTACHMENT_BYTES:
        raise ValueError(f"Attachment {raw_name!r} exceeds 25 MB")
    return _safe_path_part(raw_name, "attachment"), content


async def store_proxy_attachments(session_id: str, attachments: list[Any]) -> list[Path]:
    """Persist uploaded files for a tmux-delivered proxy message."""
    if not attachments:
        return []

    safe_session = _safe_path_part(session_id, "session")
    target_dir = get_gobby_home() / "attachments" / "attached-sessions" / safe_session
    await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)

    stored_paths: list[Path] = []
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("Attachment entries must be objects")
        safe_name, content = _decode_attachment_payload(item)
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
