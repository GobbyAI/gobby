"""Tests for session-start transcript discovery helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.hooks.event_handlers import _session_start as session_start_pkg
from gobby.hooks.event_handlers._session_start import transcripts

pytestmark = pytest.mark.unit


def test_find_json_session_transcript_does_not_fallback_to_unrelated_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = "/tmp/project"
    project_hash = hashlib.sha256(cwd.encode()).hexdigest()
    chats_dir = tmp_path / ".qwen" / "tmp" / project_hash / "chats"
    chats_dir.mkdir(parents=True)
    unrelated = chats_dir / "session-1-unrelated.json"
    unrelated.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(session_start_pkg.Path, "home", lambda: tmp_path)
    handler = SimpleNamespace(logger=MagicMock())

    result = transcripts.find_json_session_transcript(
        handler,
        "qwen",
        "Qwen",
        {"cwd": cwd, "session_id": "expected-session"},
        "expected-session",
    )

    assert result is None
