"""Tests for runner chat attachment cleanup helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.paths import get_gobby_home
from gobby.runner_maintenance import _remove_stale_chat_attachment_file
from gobby.servers.chat_attachment_cleanup import cleanup_stale_attachments_sync
from gobby.storage import chat_attachments
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit

ATTACHMENT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"


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


def test_remove_stale_chat_attachment_file_uses_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files_home = tmp_path / "files_home"
    files_home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _write_local_bootstrap(files_home)
    locator = (
        files_home
        / "_personal"
        / "attachments"
        / PERSONAL_PROJECT_ID
        / ATTACHMENT_ID[:2]
        / ATTACHMENT_ID
        / "stale.txt"
    )
    locator.parent.mkdir(parents=True)
    locator.write_text("stale")

    assert _remove_stale_chat_attachment_file(PERSONAL_PROJECT_ID, ATTACHMENT_ID, "stale.txt")
    assert not locator.exists()


def test_remote_cleanup_does_not_unlink_or_mutate_rows(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _write_remote_bootstrap()
    node_file = tmp_path / "gobby-home" / "projects" / PERSONAL_PROJECT_ID / "attachments" / "x.txt"
    node_file.parent.mkdir(parents=True)
    node_file.write_text("node-local")
    chat_attachments.create_attachment(
        temp_db,
        attachment_id=ATTACHMENT_ID,
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename="x.txt",
        mime_type="text/plain",
        size_bytes=10,
        local_path="_personal/attachments/x",
        published=True,
    )

    removed = cleanup_stale_attachments_sync(temp_db, cutoff="2000-01-01T00:00:00+00:00", limit=10)

    assert removed == []
    assert node_file.exists()
    assert temp_db.fetchone("SELECT id FROM chat_attachments WHERE id = %s", (ATTACHMENT_ID,))
