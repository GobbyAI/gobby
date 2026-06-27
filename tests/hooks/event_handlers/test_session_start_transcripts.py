"""Tests for session-start transcript discovery helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gobby.hooks.event_handlers import _session_start as session_start_pkg
from gobby.hooks.event_handlers._session_start import transcripts

pytestmark = pytest.mark.unit


def test_find_json_session_transcript_skips_candidates_that_disappear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = "/tmp/project"
    project_hash = hashlib.sha256(cwd.encode()).hexdigest()
    chats_dir = tmp_path / ".qwen" / "tmp" / project_hash / "chats"
    chats_dir.mkdir(parents=True)
    surviving = chats_dir / "session-1-surviving.json"
    vanished = chats_dir / "session-2-vanished.json"
    surviving.write_text("{}", encoding="utf-8")
    vanished.write_text("{}", encoding="utf-8")
    os.utime(surviving, (1000, 1000))
    os.utime(vanished, (2000, 2000))

    original_stat = Path.stat

    def flaky_stat(self: Path) -> os.stat_result:
        if self == vanished:
            raise FileNotFoundError(self)
        return original_stat(self)

    monkeypatch.setattr(session_start_pkg.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(Path, "stat", flaky_stat)
    handler = SimpleNamespace(logger=MagicMock())

    result = transcripts.find_json_session_transcript(
        handler,
        "qwen",
        "Qwen",
        {"cwd": cwd},
        "",
    )

    assert result == str(surviving)
