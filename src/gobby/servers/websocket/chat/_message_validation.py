"""Validation helpers for chat WebSocket message payloads."""

from __future__ import annotations

from typing import Any

ChatContent = str | list[dict[str, Any]]


def as_optional_str(value: Any) -> str | None:
    """Return value only when it is a string."""
    return value if isinstance(value, str) else None


def validate_chat_content(
    content: Any,
    content_blocks: Any,
    *,
    has_attachments: bool,
) -> tuple[ChatContent | None, str | None]:
    """Validate chat content, preferring explicit content blocks when present."""
    if content_blocks is not None:
        if not isinstance(content_blocks, list) or not content_blocks:
            return None, "Missing or invalid 'content_blocks' field"
        if not all(isinstance(block, dict) for block in content_blocks):
            return None, "Missing or invalid 'content_blocks' field"
        return list(content_blocks), None

    if not isinstance(content, str):
        return None, "Missing or invalid 'content' field"
    if content.strip() or has_attachments:
        return content, None
    return None, "Missing or invalid 'content' field"
