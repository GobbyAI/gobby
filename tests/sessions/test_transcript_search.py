"""Tests for rendered transcript search helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.sessions.transcript_renderer import ContentBlock, RenderedMessage
from gobby.sessions.transcript_search import search_rendered_messages

pytestmark = pytest.mark.unit


def _message(content: str, block_content: str | None = None) -> RenderedMessage:
    blocks = []
    if block_content is not None:
        blocks.append(ContentBlock(type="text", content=block_content))
    return RenderedMessage(
        id="msg-1",
        role="assistant",
        content=content,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        content_blocks=blocks,
    )


def test_search_rendered_messages_matches_case_insensitively() -> None:
    """Search matches content regardless of case."""
    results = search_rendered_messages(
        session_id="sess-1",
        messages=[_message("Needle in content")],
        query="needle",
        limit=5,
    )

    assert len(results) == 1
    assert results[0]["session_id"] == "sess-1"
    assert "Needle" in results[0]["snippet"]


def test_search_rendered_messages_matches_text_blocks() -> None:
    """Search includes rendered text blocks."""
    results = search_rendered_messages(
        session_id="sess-1",
        messages=[_message("summary", block_content="needle in block")],
        query="needle",
        limit=5,
    )

    assert len(results) == 1
    assert "needle in block" in results[0]["snippet"]


def test_search_rendered_messages_truncates_default_result_content() -> None:
    """Search truncates verbose message content by default."""
    long_content = "needle " + ("x" * 600)
    results = search_rendered_messages(
        session_id="sess-1",
        messages=[_message(long_content, block_content=long_content)],
        query="needle",
        limit=5,
    )

    message = results[0]["message"]
    assert "... (truncated)" in message["content"]
    assert "... (truncated)" in message["content_blocks"][0]["content"]


def test_search_rendered_messages_rejects_non_positive_limit() -> None:
    assert (
        search_rendered_messages(
            session_id="sess-1",
            messages=[_message("needle")],
            query="needle",
            limit=0,
        )
        == []
    )


def test_search_rendered_messages_uses_casefolded_needle_length() -> None:
    results = search_rendered_messages(
        session_id="sess-1",
        messages=[_message("prefix STRASSE suffix")],
        query="straße",
        limit=5,
    )

    assert len(results) == 1
    assert "STRASSE" in results[0]["snippet"]
