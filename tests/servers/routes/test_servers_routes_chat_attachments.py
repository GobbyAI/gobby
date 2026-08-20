"""Tests for stored chat attachment HTTP routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import gobby.servers.routes.chat_attachments as chat_attachment_routes
from gobby.config.app import DaemonConfig
from gobby.paths import get_gobby_home
from gobby.servers.chat_attachment_files import attachment_relative_locator
from gobby.servers.chat_attachment_limits import _store_limit, resolve_server_attachment_limits
from gobby.servers.routes.chat_attachments import (
    create_chat_attachments_router,
    resolve_mime_type,
)
from gobby.storage.chat_attachments import bind_attachments, create_attachment
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID, LocalProjectManager
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


def test_store_limit_uses_fallback_on_database_driver_error() -> None:
    class FailingStore:
        def read_snapshot(self) -> object:
            raise psycopg.OperationalError("database unavailable")

    assert _store_limit(FailingStore(), "chat.attachment_max_file_bytes", 123) == 123


def _write_local_bootstrap(files_home: Path) -> None:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        f"datastore_mode: local\nfiles_home: {files_home}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)


@pytest.fixture
def files_home(tmp_path: Path) -> Path:
    root = tmp_path / "files_home"
    root.mkdir()
    return root


@pytest.fixture
def client(
    temp_db: HubDatabase,
    tmp_path: Path,
    files_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _write_local_bootstrap(files_home)
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=None,
    )
    # Exercise the documented config_store fallback seam in
    # resolve_server_attachment_limits; the attribute is dynamic, so type
    # it through Any.
    cast(Any, server.services).config_store = ConfigStore(temp_db)
    app = FastAPI()
    app.include_router(create_chat_attachments_router(server))
    return TestClient(app)


def test_upload_persists_metadata_and_file(
    client: TestClient,
    temp_db: HubDatabase,
    files_home: Path,
) -> None:
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
    locator = row["local_path"]
    assert locator == attachment_relative_locator(project.id, payload["id"], "note.txt")
    assert not Path(locator).is_absolute()
    stored_path = files_home / locator
    assert stored_path.read_bytes() == b"hello"
    assert stored_path.parent.name == payload["id"]
    assert stored_path.parent.parent.name == payload["id"][:2]
    assert stored_path.parent.parent.parent.name == project.id
    assert stored_path.parent.parent.parent.parent.name == "attachments"
    assert stored_path.parent.parent.parent.parent.parent.name == "_personal"


def test_attachment_limits_returns_configured_max_file_bytes(
    client: TestClient,
    temp_db: HubDatabase,
) -> None:
    ConfigMutations(temp_db).patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "chat.attachment_max_file_bytes": 4,
                "chat.attachment_max_total_bytes_per_message": 4,
            }
        ),
    )

    response = client.get("/api/chat/attachments/limits")

    assert response.status_code == 200
    assert response.json() == {"max_file_bytes": 4}


def test_attachment_limits_accept_wrapped_runtime_capability() -> None:
    active = DaemonConfig(
        chat={
            "attachment_max_file_bytes": 4321,
            "attachment_max_total_bytes_per_message": 86_420,
        }
    )

    class Runtime:
        ready = True

        def capture(self) -> object:
            snapshot = type("Snapshot", (), {"active": active})()
            return type("Capture", (), {"snapshot": snapshot})()

    class Services:
        config_runtime = Runtime()

    class Server:
        services = Services()

    assert resolve_server_attachment_limits(Server()).max_file_bytes == 4321


def test_attachment_limit_runtime_capture_failure_propagates() -> None:
    class Runtime:
        ready = True

        def capture(self) -> object:
            raise RuntimeError("capture failed")

    class Services:
        config_runtime = Runtime()

    class Server:
        services = Services()

    with pytest.raises(RuntimeError, match="capture failed"):
        resolve_server_attachment_limits(Server())


def test_upload_without_project_id_uses_server_project(
    temp_db: HubDatabase,
    tmp_path: Path,
    files_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _write_local_bootstrap(files_home)
    project = LocalProjectManager(temp_db).create(name="server-project")
    server = create_http_server(
        config=DaemonConfig(),
        database=temp_db,
        session_manager=None,
        project_id=project.id,
    )
    cast(Any, server.services).config_store = ConfigStore(temp_db)
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
    ConfigMutations(temp_db).patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "chat.attachment_max_file_bytes": 4,
                "chat.attachment_max_total_bytes_per_message": 4,
            }
        ),
    )

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
    assert not Path(row["local_path"]).is_absolute()


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
    files_home: Path,
) -> None:
    attachment_id = "00000000-0000-4000-8000-000000000001"
    locator = attachment_relative_locator(PERSONAL_PROJECT_ID, attachment_id, "directory.txt")
    content_dir = files_home / locator
    content_dir.parent.mkdir(parents=True)
    content_dir.mkdir()
    create_attachment(
        temp_db,
        attachment_id=attachment_id,
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename="directory.txt",
        mime_type="text/plain",
        size_bytes=0,
        local_path=locator,
        published=True,
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
    files_home: Path,
) -> None:
    first = client.post(
        "/api/chat/attachments",
        files={"file": ("queued.txt", b"queued", "text/plain")},
    ).json()
    first_locator = temp_db.fetchone(
        "SELECT local_path FROM chat_attachments WHERE id = %s", (first["id"],)
    )["local_path"]
    first_path = files_home / first_locator

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
    files_home: Path,
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

    def fail_unlink(_locator: str) -> None:
        raise PermissionError("blocked")

    monkeypatch.setattr(
        "gobby.servers.chat_attachment_files.unlink_files_home_descendant",
        fail_unlink,
    )

    response = client.delete(f"/api/chat/attachments/{uploaded['id']}")

    assert response.status_code == 200
    assert response.json() == {"ok": False}
    remaining = temp_db.fetchone(
        "SELECT claim_token FROM chat_attachments WHERE id = %s", (uploaded["id"],)
    )
    assert remaining is not None
    assert remaining["claim_token"] is None


def test_vanished_files_home_raises_and_does_not_recreate(
    client: TestClient,
    files_home: Path,
) -> None:
    # client is requested for its environment setup side effect.
    from gobby.paths import FilesHomeError, require_files_home

    require_files_home()
    vanished = files_home.parent / "vanished"
    files_home.rename(vanished)
    with pytest.raises(FilesHomeError):
        require_files_home()
    assert not files_home.exists()


def test_replaced_files_home_raises(
    client: TestClient,
    files_home: Path,
) -> None:
    # client is requested for its environment setup side effect.
    from gobby.paths import FilesHomeError, publish_files_home_descendant, require_files_home

    require_files_home()
    replacement = files_home.parent / "replacement"
    replacement.mkdir()
    files_home.rename(files_home.parent / "old-home")
    replacement.rename(files_home)
    with pytest.raises(FilesHomeError):
        publish_files_home_descendant("USER.md", b"nope")


@pytest.mark.asyncio
async def test_upload_cancel_before_replace_leaves_no_row(
    temp_db: HubDatabase,
    files_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import asyncio
    import threading

    from fastapi import UploadFile

    import gobby.servers.chat_attachment_upload as upload

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _write_local_bootstrap(files_home)
    started = threading.Event()
    release = threading.Event()

    def blocking_replace(*_args: object, **_kwargs: object) -> None:
        started.set()
        release.wait(timeout=5)
        raise RuntimeError("replace aborted")

    monkeypatch.setattr(upload, "durable_replace_files_home", blocking_replace)

    class _Upload:
        filename = "note.txt"
        content_type = "text/plain"
        size = 5

        async def read(self, _size: int) -> bytes:
            if not getattr(self, "_sent", False):
                self._sent = True
                return b"hello"
            return b""

    task = asyncio.create_task(
        upload.publish_uploaded_attachment(
            file=cast(UploadFile, _Upload()),
            resolved_project_id=PERSONAL_PROJECT_ID,
            attachment_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            filename="note.txt",
            mime_type="text/plain",
            draft_id=None,
            max_file_bytes=1000,
            database=temp_db,
            run_db=_run_db,
            validate_mime=_noop_mime,
            ensure_disk_space=lambda *_a, **_k: None,
        )
    )
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = temp_db.fetchone(
        "SELECT id FROM chat_attachments WHERE id = %s",
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
    )
    assert row is None


async def _run_db(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


async def _noop_mime(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_cancel_after_published_cas_keeps_row(
    temp_db: HubDatabase,
    files_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import asyncio

    from fastapi import UploadFile

    import gobby.servers.chat_attachment_upload as upload

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _write_local_bootstrap(files_home)
    calls = {"cas": 0}

    async def tracking_shielded(name: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        from gobby.servers.chat_attachment_workers import ShieldedOutcome

        if name == "attachment-publish-cas":
            calls["cas"] += 1
            result = fn(*args, **kwargs)
            return ShieldedOutcome(result=result), True
        result = fn(*args, **kwargs)
        return ShieldedOutcome(result=result), False

    monkeypatch.setattr(upload, "run_shielded", tracking_shielded)

    class _Upload:
        filename = "note.txt"
        content_type = "text/plain"
        size = 5

        async def read(self, _size: int) -> bytes:
            if not getattr(self, "_sent", False):
                self._sent = True
                return b"hello"
            return b""

    with pytest.raises(asyncio.CancelledError):
        await upload.publish_uploaded_attachment(
            file=cast(UploadFile, _Upload()),
            resolved_project_id=PERSONAL_PROJECT_ID,
            attachment_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            filename="note.txt",
            mime_type="text/plain",
            draft_id=None,
            max_file_bytes=1000,
            database=temp_db,
            run_db=_run_db,
            validate_mime=_noop_mime,
            ensure_disk_space=lambda *_a, **_k: None,
        )
    row = temp_db.fetchone(
        "SELECT published FROM chat_attachments WHERE id = %s",
        ("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",),
    )
    assert row is not None
    assert row["published"] is True
    assert calls["cas"] == 1


@pytest.mark.asyncio
async def test_unpublished_after_replace_is_removed_by_restart_cleanup(
    temp_db: HubDatabase,
    files_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    from gobby.servers.chat_attachment_cleanup import cleanup_stale_attachments_sync

    monkeypatch.setenv("GOBBY_HOME", str(tmp_path / "gobby-home"))
    _write_local_bootstrap(files_home)
    record = create_attachment(
        temp_db,
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename="note.txt",
        mime_type="text/plain",
        size_bytes=5,
        local_path=attachment_relative_locator(
            PERSONAL_PROJECT_ID, "cccccccc-cccc-cccc-cccc-cccccccccccc", "note.txt"
        ),
        attachment_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        published=False,
        claim_token="dddddddd-dddd-dddd-dddd-dddddddddddd",
    )
    dest = files_home / record.local_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"hello")
    stale_at = datetime.now(UTC) - timedelta(hours=1)
    with temp_db.transaction() as conn:
        conn.execute(
            "UPDATE chat_attachments SET claimed_at = %s WHERE id = %s",
            (stale_at, record.id),
        )
    removed = cleanup_stale_attachments_sync(
        temp_db,
        cutoff=datetime.now(UTC) + timedelta(days=1),
        limit=10,
    )
    assert any(item.id == record.id for item in removed)
    row = temp_db.fetchone(
        "SELECT id FROM chat_attachments WHERE id = %s",
        (record.id,),
    )
    assert row is None
    assert not dest.exists()
