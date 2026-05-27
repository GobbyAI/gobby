"""Tests for stored chat attachment HTTP routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gobby.servers.routes.chat_attachments as chat_attachment_routes
from gobby.config.app import DaemonConfig
from gobby.servers.routes.chat_attachments import (
    create_chat_attachments_router,
    resolve_mime_type,
)
from gobby.storage.chat_attachments import bind_attachments, create_attachment
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


@pytest.fixture
def client(
    temp_db: HubDatabase,
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


def test_upload_persists_metadata_and_file(client: TestClient, temp_db: HubDatabase) -> None:
    project = LocalProjectManager(temp_db).create(name="attachment-project")

    response = client.post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"draft_id": "draft-1", "project_id": project.id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == project.id
    assert payload["filename"] == "note.txt"
    assert payload["mime_type"] == "text/plain"
    assert payload["size_bytes"] == 5
    assert payload["content_url"] == f"/api/chat/attachments/{payload['id']}/content"

    row = temp_db.fetchone("SELECT * FROM chat_attachments WHERE id = %s", (payload["id"],))
    assert row is not None
    assert row["project_id"] == project.id
    assert row["draft_id"] == "draft-1"
    stored_path = Path(row["local_path"])
    assert stored_path.read_bytes() == b"hello"
    assert stored_path.parent.name == payload["id"]
    assert stored_path.parent.parent.name == payload["id"][:2]
    assert stored_path.parent.parent.parent.name == "attachments"
    assert stored_path.parent.parent.parent.parent.name == project.id


def test_upload_without_project_id_uses_server_project(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    project = LocalProjectManager(temp_db).create(name="server-project")
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=None,
        project_id=project.id,
    )
    server.services.config_store = ConfigStore(temp_db)
    app = FastAPI()
    app.include_router(create_chat_attachments_router(server))

    response = TestClient(app).post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == project.id
    row = temp_db.fetchone(
        "SELECT project_id FROM chat_attachments WHERE id = %s", (payload["id"],)
    )
    assert row is not None
    assert row["project_id"] == project.id


def test_upload_without_project_id_falls_back_to_personal(client: TestClient) -> None:
    response = client.post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["project_id"] == PERSONAL_PROJECT_ID


def test_upload_rejects_unknown_project_id_without_persisting(
    client: TestClient,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    response = client.post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
        data={"project_id": "missing-project"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown project_id"
    assert temp_db.fetchall("SELECT * FROM chat_attachments") == []
    assert not (tmp_path / "gobby-home" / "projects").exists()


def test_upload_uses_config_store_limit_and_skips_metadata_on_oversize(
    client: TestClient,
    temp_db: HubDatabase,
) -> None:
    ConfigStore(temp_db).set("chat.attachment_max_file_bytes", 4)

    response = client.post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 413
    assert temp_db.fetchall("SELECT * FROM chat_attachments") == []


def test_upload_sanitizes_traversal_filename_preserving_extension(
    client: TestClient,
    temp_db: HubDatabase,
) -> None:
    response = client.post(
        "/api/chat/attachments",
        files={"file": ("../note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    payload = response.json()
    row = temp_db.fetchone(
        "SELECT local_path FROM chat_attachments WHERE id = %s", (payload["id"],)
    )
    assert row is not None
    assert Path(row["local_path"]).name == "_note.txt"


def test_upload_truncates_oversized_filename_suffix_to_safe_path_part(
    client: TestClient,
    temp_db: HubDatabase,
) -> None:
    filename = f"a.{'x' * 300}"

    response = client.post(
        "/api/chat/attachments",
        files={"file": (filename, b"hello", "text/plain")},
    )

    assert response.status_code == 200
    payload = response.json()
    row = temp_db.fetchone(
        "SELECT local_path FROM chat_attachments WHERE id = %s", (payload["id"],)
    )
    assert row is not None
    stored_name = Path(row["local_path"]).name
    assert len(stored_name.encode("utf-8")) <= 255
    assert stored_name == payload["filename"]


def test_upload_checks_disk_space_once_using_known_file_size(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_sizes: list[int] = []

    def fake_ensure_disk_space(_directory: Path, incoming_bytes: int) -> None:
        checked_sizes.append(incoming_bytes)

    monkeypatch.setattr(chat_attachment_routes, "_ensure_disk_space", fake_ensure_disk_space)

    response = client.post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    assert checked_sizes == [5]


def test_upload_rejects_mismatched_mime_without_persisting(
    client: TestClient,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    response = client.post(
        "/api/chat/attachments",
        files={"file": ("screen.png", b"%PDF-1.7\nbody", "image/png")},
    )

    assert response.status_code == 415
    assert "application/pdf" in response.json()["detail"]
    assert temp_db.fetchall("SELECT * FROM chat_attachments") == []
    assert not any((tmp_path / "gobby-home").rglob("screen.png"))


def test_resolve_mime_type_prefers_content_type_and_guesses_filename() -> None:
    assert resolve_mime_type("text/plain; charset=utf-8", "data.bin") == "text/plain"
    assert resolve_mime_type(None, "screen.png") == "image/png"


def test_upload_rejects_late_invalid_utf8_text(
    client: TestClient,
    temp_db: HubDatabase,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("WARNING", logger="gobby.servers.routes.chat_attachments")
    payload = (b"a" * 600) + b"\xff"

    response = client.post(
        "/api/chat/attachments",
        files={"file": ("bad.txt", payload, "text/plain")},
    )

    assert response.status_code == 415
    assert "not valid UTF-8 text" in response.json()["detail"]
    assert "bad.txt" in caplog.text
    assert "attachment_id=" in caplog.text
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
    content_by_type = {
        "image/png": b"\x89PNG\r\n\x1a\npayload",
        "application/pdf": b"%PDF-1.7\npayload",
        "text/plain": b"hello",
    }
    created = client.post(
        "/api/chat/attachments",
        files={"file": (filename, content_by_type[content_type], content_type)},
    ).json()

    response = client.get(f"/api/chat/attachments/{created['id']}/content")

    assert response.status_code == 200
    assert response.content == content_by_type[content_type]
    assert response.headers["content-disposition"].startswith(expected_disposition)


def test_content_route_returns_404_when_content_path_is_directory(
    client: TestClient,
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    content_dir = tmp_path / "directory-backed-attachment"
    content_dir.mkdir()
    create_attachment(
        temp_db,
        attachment_id="00000000-0000-4000-8000-000000000001",
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename="directory.txt",
        mime_type="text/plain",
        size_bytes=0,
        local_path=str(content_dir),
    )

    response = client.get("/api/chat/attachments/00000000-0000-4000-8000-000000000001/content")

    assert response.status_code == 404
    assert response.json()["detail"] == "Attachment content not found"


def test_content_route_rejects_invalid_attachment_id(client: TestClient) -> None:
    response = client.get("/api/chat/attachments/not-a-uuid/content")

    assert response.status_code == 404
    assert response.json()["detail"] == "Invalid attachment_id"


def test_delete_only_removes_unbound_uploads(
    client: TestClient,
    temp_db: HubDatabase,
) -> None:
    first = client.post(
        "/api/chat/attachments",
        files={"file": ("queued.txt", b"queued", "text/plain")},
    ).json()
    first_path = Path(
        temp_db.fetchone("SELECT local_path FROM chat_attachments WHERE id = %s", (first["id"],))[
            "local_path"
        ]
    )

    delete_response = client.delete(f"/api/chat/attachments/{first['id']}")

    assert delete_response.status_code == 200
    assert temp_db.fetchone("SELECT * FROM chat_attachments WHERE id = %s", (first["id"],)) is None
    assert not first_path.exists()

    second = client.post(
        "/api/chat/attachments",
        files={"file": ("bound.txt", b"bound", "text/plain")},
    ).json()
    bind_attachments(temp_db, [second["id"]], conversation_id="conv-1", message_id="msg-1")

    bound_delete = client.delete(f"/api/chat/attachments/{second['id']}")

    assert bound_delete.status_code == 409


def test_delete_reports_file_removal_failure_after_metadata_delete(
    client: TestClient,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uploaded = client.post(
        "/api/chat/attachments",
        files={"file": ("queued.txt", b"queued", "text/plain")},
    ).json()
    uploaded_row = temp_db.fetchone(
        "SELECT local_path FROM chat_attachments WHERE id = %s",
        (uploaded["id"],),
    )
    assert uploaded_row is not None
    target_path = Path(uploaded_row["local_path"])
    original_unlink = Path.unlink

    def guarded_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == target_path:
            raise PermissionError(str(self))
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", guarded_unlink)

    response = client.delete(f"/api/chat/attachments/{uploaded['id']}")

    assert response.status_code == 200
    assert response.json() == {"ok": False}
    assert (
        temp_db.fetchone("SELECT * FROM chat_attachments WHERE id = %s", (uploaded["id"],)) is None
    )
