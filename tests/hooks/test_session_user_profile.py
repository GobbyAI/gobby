"""Tests for global user profile session seeding."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.hooks.event_handlers._session_start.profile import (
    read_user_profile_content,
    seed_user_profile_content,
)

pytestmark = pytest.mark.unit


class FakeSessionVariableManager:
    calls: list[tuple[str, dict[str, str]]] = []

    def __init__(self, db: object) -> None:
        self.db = db

    def merge_variables(self, session_id: str, variables: dict[str, str]) -> None:
        self.calls.append((session_id, variables))


def test_read_user_profile_content_reads_personal_user_md(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    profile_dir = tmp_path / "personal"
    profile_dir.mkdir()
    (profile_dir / "USER.md").write_text("\n## Identity\nJosh\n\n", encoding="utf-8")

    assert read_user_profile_content() == "## Identity\nJosh"


def test_seed_user_profile_content_merges_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    profile_dir = tmp_path / "personal"
    profile_dir.mkdir()
    (profile_dir / "USER.md").write_text("## Preferences\nConcise.", encoding="utf-8")
    handler = SimpleNamespace(_session_manager=SimpleNamespace(db=object()))
    FakeSessionVariableManager.calls = []

    with patch("gobby.workflows.state_manager.SessionVariableManager", FakeSessionVariableManager):
        seed_user_profile_content(handler, "session-1")

    assert FakeSessionVariableManager.calls == [
        ("session-1", {"user_profile_content": "## Preferences\nConcise."})
    ]


def test_seed_user_profile_content_clears_missing_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    handler = SimpleNamespace(_session_manager=SimpleNamespace(db=object()))
    FakeSessionVariableManager.calls = []

    with patch("gobby.workflows.state_manager.SessionVariableManager", FakeSessionVariableManager):
        seed_user_profile_content(handler, "session-1")

    assert FakeSessionVariableManager.calls == [("session-1", {"user_profile_content": ""})]
