"""Tests for web chat message HTTP routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.chat import create_chat_router
from gobby.storage import chat_attachments, chat_messages
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def test_delete_messages_removes_message_bound_attachment_file(
    temp_db: LocalDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gobby_home = tmp_path / "gobby-home"
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    server = create_http_server(
        database=temp_db,
        session_manager=SessionManager(temp_db),
    )
    app = FastAPI()
    app.include_router(create_chat_router(server))

    attachment_path = (
        gobby_home / "projects" / PERSONAL_PROJECT_ID / "attachments" / "attachment.txt"
    )
    attachment_path.parent.mkdir(parents=True)
    attachment_path.write_text("uploaded")
    attachment = chat_attachments.create_attachment(
        temp_db,
        attachment_id="attachment-1",
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename=attachment_path.name,
        mime_type="text/plain",
        size_bytes=attachment_path.stat().st_size,
        local_path=str(attachment_path),
    )
    message_id = chat_messages.save_message(
        temp_db,
        conversation_id="conv-1",
        role="user",
        content="hello",
    )
    chat_attachments.bind_attachments(temp_db, [attachment.id], message_id=message_id)

    response = TestClient(app).delete("/api/chat/conv-1/messages")

    assert response.status_code == 200
    assert response.json() == {"deleted": 1}
    assert not attachment_path.exists()
    assert chat_attachments.get_attachment(temp_db, attachment.id) is None
    assert temp_db.fetchone("SELECT * FROM chat_messages WHERE id = ?", (message_id,)) is None


def test_delete_messages_skips_attachment_paths_outside_managed_storage(
    temp_db: LocalDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    server = create_http_server(
        database=temp_db,
        session_manager=SessionManager(temp_db),
    )
    app = FastAPI()
    app.include_router(create_chat_router(server))

    attachment_path = tmp_path / "outside.txt"
    attachment_path.write_text("uploaded")
    attachment = chat_attachments.create_attachment(
        temp_db,
        attachment_id="attachment-unsafe",
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename=attachment_path.name,
        mime_type="text/plain",
        size_bytes=attachment_path.stat().st_size,
        local_path=str(attachment_path),
    )
    message_id = chat_messages.save_message(
        temp_db,
        conversation_id="conv-unsafe",
        role="user",
        content="hello",
    )
    chat_attachments.bind_attachments(temp_db, [attachment.id], message_id=message_id)

    response = TestClient(app).delete("/api/chat/conv-unsafe/messages")

    assert response.status_code == 200
    assert attachment_path.exists()
    assert chat_attachments.get_attachment(temp_db, attachment.id) is None
