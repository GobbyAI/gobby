"""Transcript normalization regression tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.sessions.transcript_normalization import normalize_transcript_records
from gobby.sessions.transcripts.base import ParsedMessage

pytestmark = pytest.mark.unit


def test_normalize_transcript_records_ignores_non_string_grok_update_type() -> None:
    message = ParsedMessage(
        index=0,
        role="tool",
        content="",
        content_type="tool_result",
        tool_name="PostToolUse",
        tool_input=None,
        tool_result=None,
        timestamp=datetime.now(UTC),
        raw_json={"sessionUpdate": 123, "type": {"name": "bad"}},
    )

    assert normalize_transcript_records([message], "grok") == [message]
