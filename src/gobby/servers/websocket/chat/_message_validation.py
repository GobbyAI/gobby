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
        if not isinstance(content_blocks, list):
            return None, "Invalid 'content_blocks' field: expected a list"
        if not content_blocks:
            return None, "Invalid 'content_blocks' field: expected at least one block"
        if not all(isinstance(block, dict) for block in content_blocks):
            return None, "Invalid 'content_blocks' field: each block must be an object"
        return list(content_blocks), None

    if not isinstance(content, str):
        return None, "Missing 'content' or 'content_blocks' field"
    if content.strip() or has_attachments:
        return content, None
    return None, "Missing or invalid 'content' field"
