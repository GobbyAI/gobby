"""Path-shape detection for transcript sources."""

from __future__ import annotations

import pytest

from gobby.sessions.transcript_source import _detect_source_from_path

pytestmark = pytest.mark.unit


def test_detect_source_from_path_agy_brain_layout() -> None:
    path = (
        "/Users/me/.gemini/antigravity-cli/brain/"
        "conv-1/.system_generated/logs/transcript_full.jsonl"
    )
    assert _detect_source_from_path(path) == "agy"


def test_detect_source_from_path_unknown_shape_is_none() -> None:
    assert _detect_source_from_path("/tmp/random/session.jsonl") is None
    assert _detect_source_from_path("~/.gemini/google-accounts.json") is None
    assert _detect_source_from_path(None) is None
