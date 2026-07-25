"""Telegram sticker payload normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def telegram_sticker_attachments(
    msg_data: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str | None]] | None:
    """Normalize one supported Telegram Sticker object and its vision preview."""
    raw_sticker = msg_data.get("sticker")
    if not isinstance(raw_sticker, Mapping):
        return None

    file_id = raw_sticker.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None

    is_animated = raw_sticker.get("is_animated") is True
    is_video = raw_sticker.get("is_video") is True
    if is_animated and is_video:
        return None

    if is_animated:
        format_name = "animated"
        content_type = "application/x-tgsticker"
        extension = "tgs"
    elif is_video:
        format_name = "video"
        content_type = "video/webm"
        extension = "webm"
    else:
        format_name = "static"
        content_type = "image/webp"
        extension = "webp"

    unique_id = _identifier(raw_sticker, file_id)
    attachments = [
        {
            "file_id": file_id,
            "filename": f"sticker_{unique_id}.{extension}",
            "content_type": content_type,
            "size_bytes": _file_size(raw_sticker.get("file_size")),
            "media_type": "sticker",
        }
    ]
    if format_name != "static":
        thumbnail = raw_sticker.get("thumbnail")
        if isinstance(thumbnail, Mapping):
            thumbnail_id = thumbnail.get("file_id")
            if isinstance(thumbnail_id, str) and thumbnail_id:
                thumbnail_unique_id = _identifier(thumbnail, thumbnail_id)
                attachments.append(
                    {
                        "file_id": thumbnail_id,
                        "filename": f"sticker_thumbnail_{thumbnail_unique_id}.webp",
                        "content_type": "image/webp",
                        "size_bytes": _file_size(thumbnail.get("file_size")),
                        "media_type": "sticker_thumbnail",
                    }
                )

    return attachments, {
        "format": format_name,
        "emoji": _optional_string(raw_sticker.get("emoji")),
        "set_name": _optional_string(raw_sticker.get("set_name")),
        "type": _optional_string(raw_sticker.get("type")),
    }


def _identifier(data: Mapping[str, Any], file_id: str) -> str:
    unique_id = data.get("file_unique_id")
    return unique_id if isinstance(unique_id, str) and unique_id else file_id


def _file_size(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = ["telegram_sticker_attachments"]
