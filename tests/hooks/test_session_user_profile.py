"""Tests for global user profile session seeding."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.hooks.event_handlers._session_start.profile import (
    read_user_profile_content,
    seed_user_profile_content,
)

pytestmark = pytest.mark.unit


class FakeSessionVariableManager:
    def __init__(self, db: object) -> None:
        self.db = db
        self.calls: list[tuple[str, dict[str, str]]] = []

    def merge_variables(self, session_id: str, variables: dict[str, str]) -> None:
        self.calls.append((session_id, variables))


def test_read_user_profile_content_reads_personal_user_md(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    profile_dir = tmp_path / "personal"
    profile_dir.mkdir()
    (profile_dir / "USER.md").write_text("\n## Identity\nJosh\n\n", encoding="utf-8")

    assert read_user_profile_content() == "## Identity\nJosh"


def test_seed_user_profile_content_merges_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    profile_dir = tmp_path / "personal"
    profile_dir.mkdir()
    (profile_dir / "USER.md").write_text("## Preferences\nConcise.", encoding="utf-8")
    session_manager = SimpleNamespace(db=object())
    handler = SimpleNamespace(get_session_manager=lambda: session_manager)
    captured: list[FakeSessionVariableManager] = []

    def capture_manager(db: object) -> FakeSessionVariableManager:
        manager = FakeSessionVariableManager(db)
        captured.append(manager)
        return manager

    with patch(
        "gobby.hooks.event_handlers._session_start.profile.SessionVariableManager",
        capture_manager,
    ):
        seed_user_profile_content(handler, "session-1")

    assert captured[0].calls == [
        ("session-1", {"user_profile_content": "## Preferences\nConcise."})
    ]


def test_seed_user_profile_content_clears_missing_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    session_manager = SimpleNamespace(db=object())
    handler = SimpleNamespace(get_session_manager=lambda: session_manager)
    captured: list[FakeSessionVariableManager] = []

    def capture_manager(db: object) -> FakeSessionVariableManager:
        manager = FakeSessionVariableManager(db)
        captured.append(manager)
        return manager

    with patch(
        "gobby.hooks.event_handlers._session_start.profile.SessionVariableManager",
        capture_manager,
    ):
        seed_user_profile_content(handler, "session-1")

    assert captured[0].calls == [("session-1", {"user_profile_content": ""})]
