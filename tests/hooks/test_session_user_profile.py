"""Tests for global user profile session seeding."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.hooks.event_handlers._session_start.profile import (
    UserProfileError,
    read_user_profile_content,
    seed_user_profile_content,
    write_user_profile_content,
)
from gobby.paths import get_gobby_home

pytestmark = pytest.mark.unit


def _write_local_bootstrap(files_home: Path) -> None:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        f"datastore_mode: local\nfiles_home: {files_home}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)


def _write_remote_bootstrap() -> None:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        "datastore_mode: remote\nhub_daemon_url: http://hub.example.test:60887\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)


@pytest.fixture(autouse=True)
def _restore_bootstrap() -> Iterator[None]:
    bootstrap = get_gobby_home() / "bootstrap.yaml"
    previous = bootstrap.read_bytes() if bootstrap.exists() else None
    yield
    if previous is None:
        bootstrap.unlink(missing_ok=True)
    else:
        bootstrap.write_bytes(previous)
        bootstrap.chmod(0o600)


class FakeSessionVariableManager:
    def __init__(self, db: object) -> None:
        self.db = db
        self.calls: list[tuple[str, dict[str, str]]] = []

    def merge_variables(self, session_id: str, variables: dict[str, str]) -> None:
        self.calls.append((session_id, variables))


def test_read_user_profile_content_reads_personal_user_md(
    tmp_path: Path,
) -> None:
    files_home = tmp_path / "files_home"
    files_home.mkdir()
    _write_local_bootstrap(files_home)
    daemon_home_profile = get_gobby_home() / "personal"
    daemon_home_profile.mkdir(exist_ok=True)
    (daemon_home_profile / "USER.md").write_text("daemon-home profile", encoding="utf-8")
    (files_home / "USER.md").write_text("\n## Identity\nJosh\n\n", encoding="utf-8")

    assert read_user_profile_content() == "## Identity\nJosh"

    _write_remote_bootstrap()
    with patch(
        "gobby.hooks.event_handlers._session_start.profile._read_remote_profile",
        return_value="hub profile",
    ) as fetch:
        assert read_user_profile_content() == "hub profile"
        fetch.assert_called_once()
    assert (daemon_home_profile / "USER.md").read_text(encoding="utf-8") == "daemon-home profile"


def test_seed_user_profile_content_merges_profile(
    tmp_path: Path,
) -> None:
    files_home = tmp_path / "files_home"
    files_home.mkdir()
    _write_local_bootstrap(files_home)
    (files_home / "USER.md").write_text("## Preferences\nConcise.", encoding="utf-8")
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
    tmp_path: Path,
) -> None:
    files_home = tmp_path / "files_home"
    files_home.mkdir()
    _write_local_bootstrap(files_home)
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


def test_seed_user_profile_content_fetches_hub_on_remote(tmp_path: Path) -> None:
    _write_remote_bootstrap()
    daemon_home = get_gobby_home() / "personal"
    daemon_home.mkdir(exist_ok=True)
    (daemon_home / "USER.md").write_text("node-local", encoding="utf-8")
    session_manager = SimpleNamespace(db=object())
    handler = SimpleNamespace(get_session_manager=lambda: session_manager)
    captured: list[FakeSessionVariableManager] = []

    def capture_manager(db: object) -> FakeSessionVariableManager:
        manager = FakeSessionVariableManager(db)
        captured.append(manager)
        return manager

    with (
        patch(
            "gobby.hooks.event_handlers._session_start.profile.SessionVariableManager",
            capture_manager,
        ),
        patch(
            "gobby.hooks.event_handlers._session_start.profile._read_remote_profile",
            return_value="## Preferences\nFrom hub",
        ) as fetch,
    ):
        seed_user_profile_content(handler, "session-1")

    fetch.assert_called_once()
    assert captured[0].calls == [
        ("session-1", {"user_profile_content": "## Preferences\nFrom hub"})
    ]
    assert (daemon_home / "USER.md").read_text(encoding="utf-8") == "node-local"


def test_remote_profile_read_requires_200_string_content(tmp_path: Path) -> None:
    _write_remote_bootstrap()
    with patch(
        "gobby.hooks.event_handlers._session_start.profile._fetch_user_md",
        return_value=(404, {"content": ""}),
    ):
        with pytest.raises(UserProfileError):
            read_user_profile_content()
    with patch(
        "gobby.hooks.event_handlers._session_start.profile._fetch_user_md",
        return_value=(200, {"content": ""}),
    ):
        assert read_user_profile_content() == ""
    with patch(
        "gobby.hooks.event_handlers._session_start.profile._fetch_user_md",
        return_value=(200, {"content": 12}),
    ):
        with pytest.raises(UserProfileError):
            read_user_profile_content()


def test_write_user_profile_content_local_and_remote(tmp_path: Path) -> None:
    files_home = tmp_path / "files_home"
    files_home.mkdir()
    _write_local_bootstrap(files_home)
    write_user_profile_content("local write")
    assert (files_home / "USER.md").read_text(encoding="utf-8") == "local write"

    _write_remote_bootstrap()
    with patch(
        "gobby.hooks.event_handlers._session_start.profile._write_remote_profile",
    ) as put:
        write_user_profile_content("remote write")
        put.assert_called_once_with("remote write")
    node_profile = get_gobby_home() / "personal" / "USER.md"
    if node_profile.exists():
        assert node_profile.read_text(encoding="utf-8") != "remote write"
