"""Tests for stored chat attachment HTTP routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.servers.routes.chat_attachments import create_chat_attachments_router
from gobby.storage.chat_attachments import bind_attachments
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def client(
    temp_db: LocalDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=None,
    )
    server.services.config_store = ConfigStore(temp_db)
    app = FastAPI()
    app.include_router(create_chat_attachments_router(server))
    return TestClient(app)


def test_upload_persists_metadata_and_file(client: TestClient, temp_db: LocalDatabase) -> None:
    response = client.post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"draft_id": "draft-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "note.txt"
    assert payload["mime_type"] == "text/plain"
    assert payload["size_bytes"] == 5
    assert payload["content_url"] == f"/api/chat/attachments/{payload['id']}/content"

    row = temp_db.fetchone("SELECT * FROM chat_attachments WHERE id = ?", (payload["id"],))
    assert row is not None
    assert row["draft_id"] == "draft-1"
    assert Path(row["local_path"]).read_bytes() == b"hello"


def test_upload_uses_config_store_limit_and_skips_metadata_on_oversize(
    client: TestClient,
    temp_db: LocalDatabase,
) -> None:
    ConfigStore(temp_db).set("chat.attachment_max_file_bytes", 4)

    response = client.post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 413
    assert temp_db.fetchall("SELECT * FROM chat_attachments") == []


@pytest.mark.parametrize(
    ("filename", "content_type", "expected_disposition"),
    [
        ("screen.png", "image/png", "inline"),
        ("plan.pdf", "application/pdf", "inline"),
        ("note.txt", "text/plain", "attachment"),
    ],
)
def test_content_route_sets_disposition_by_mime_type(
    client: TestClient,
    filename: str,
    content_type: str,
    expected_disposition: str,
) -> None:
    created = client.post(
        "/api/chat/attachments",
        files={"file": (filename, b"hello", content_type)},
    ).json()

    response = client.get(f"/api/chat/attachments/{created['id']}/content")

    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["content-disposition"].startswith(expected_disposition)


def test_delete_only_removes_unbound_uploads(
    client: TestClient,
    temp_db: LocalDatabase,
) -> None:
    first = client.post(
        "/api/chat/attachments",
        files={"file": ("queued.txt", b"queued", "text/plain")},
    ).json()
    first_path = Path(
        temp_db.fetchone("SELECT local_path FROM chat_attachments WHERE id = ?", (first["id"],))[
            "local_path"
        ]
    )

    delete_response = client.delete(f"/api/chat/attachments/{first['id']}")

    assert delete_response.status_code == 200
    assert temp_db.fetchone("SELECT * FROM chat_attachments WHERE id = ?", (first["id"],)) is None
    assert not first_path.exists()

    second = client.post(
        "/api/chat/attachments",
        files={"file": ("bound.txt", b"bound", "text/plain")},
    ).json()
    bind_attachments(temp_db, [second["id"]], conversation_id="conv-1", message_id="msg-1")

    bound_delete = client.delete(f"/api/chat/attachments/{second['id']}")

    assert bound_delete.status_code == 409
