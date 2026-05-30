"""Search helpers for rendered session transcripts."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.sessions.transcript_renderer import RenderedMessage

MATCH_BLOCK_TYPES: set[str] = {"text", "thinking"}
SNIPPET_RADIUS: int = 80
TRUNCATE_LIMIT: int = 500


def search_rendered_messages(
    *,
    session_id: str,
    messages: list[RenderedMessage],
    query: str,
    limit: int,
    full_content: bool = False,
) -> list[dict[str, Any]]:
    """Return message matches for a case-insensitive substring query."""
    needle = query.casefold()
    if not needle:
        return []

    results: list[dict[str, Any]] = []
    for message in messages:
        message_dict = message.to_dict()
        search_text = _message_search_text(message_dict)
        match_index = search_text.casefold().find(needle)
        if match_index == -1:
            continue

        result_message = copy.deepcopy(message_dict)
        if not full_content:
            _truncate_message(result_message)

        results.append(
            {
                "session_id": session_id,
                "message": result_message,
                "snippet": _make_snippet(search_text, match_index, len(query)),
            }
        )
        if len(results) >= limit:
            break

    return results


def _message_search_text(message: dict[str, Any]) -> str:
    """Build searchable text from rendered message content."""
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        parts.append(content)

    blocks = message.get("content_blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            block_content = block.get("content")
            if block_type in MATCH_BLOCK_TYPES and isinstance(block_content, str):
                parts.append(block_content)

    return "\n".join(parts)


def _make_snippet(text: str, match_index: int, query_length: int) -> str:
    """Return a compact snippet around a match."""
    start = max(0, match_index - SNIPPET_RADIUS)
    end = min(len(text), match_index + query_length + SNIPPET_RADIUS)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "... " + snippet
    if end < len(text):
        snippet += " ..."
    return snippet


def _truncate_message(message: dict[str, Any]) -> None:
    """Apply message truncation used by MCP transcript tools."""
    content = message.get("content")
    if isinstance(content, str) and len(content) > TRUNCATE_LIMIT:
        message["content"] = content[:TRUNCATE_LIMIT] + "... (truncated)"

    blocks = message.get("content_blocks")
    if not isinstance(blocks, list):
        return

    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in MATCH_BLOCK_TYPES:
            continue
        block_content = block.get("content")
        if isinstance(block_content, str) and len(block_content) > TRUNCATE_LIMIT:
            block["content"] = block_content[:TRUNCATE_LIMIT] + "... (truncated)"
