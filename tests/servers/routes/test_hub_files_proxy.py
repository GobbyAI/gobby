"""Hub files proxy: USER.md, hop bound, remote wiki/attachment dispatch."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.responses import Response

from gobby.config.app import DaemonConfig
from gobby.files_home_http import (
    FILES_PROXY_HOP_HEADER,
    USER_MD_CONTENT_MAX_BYTES,
    USER_MD_PATH,
    USER_MD_WIRE_MAX_BYTES,
)
from gobby.paths import get_gobby_home
from gobby.servers.routes.hub_files_proxy import create_hub_files_proxy_router
from gobby.utils.daemon_client import (
    DaemonAuthenticationError,
    DaemonClient,
    DaemonStatusError,
    DaemonTimeoutError,
)
from tests.servers.conftest import create_http_server

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


def _write_remote_bootstrap(url: str = "http://hub.example.test:60887") -> None:
    home = get_gobby_home()
    home.mkdir(parents=True, exist_ok=True)
    bootstrap = home / "bootstrap.yaml"
    bootstrap.write_text(
        f"datastore_mode: remote\nhub_daemon_url: {url}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "gobby-home"
    home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "user-home"))
    (tmp_path / "user-home").mkdir()
    return home


@pytest.fixture
def files_home(tmp_path: Path, isolated_home: Path) -> Path:
    del isolated_home
    root = tmp_path / "files_home"
    root.mkdir()
    _write_local_bootstrap(root)
    return root


def _owner_client() -> TestClient:
    app = FastAPI()
    app.include_router(create_hub_files_proxy_router())
    return TestClient(app)


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_async_client(transport: httpx.MockTransport) -> Any:
    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    return patch.object(httpx, "AsyncClient", side_effect=factory)


def test_assembled_app_exposes_user_md_routes(isolated_home: Path) -> None:
    del isolated_home
    server = create_http_server(config=DaemonConfig())
    methods: set[str] = set()
    for route in server.app.routes:
        if getattr(route, "path", "") != USER_MD_PATH:
            continue
        methods.update(getattr(route, "methods", set()) or set())
    assert {"GET", "PUT"} <= methods


def test_get_user_md_returns_files_home_profile(files_home: Path) -> None:
    (files_home / "USER.md").write_text("## Identity\nJosh\n", encoding="utf-8")
    response = _owner_client().get(USER_MD_PATH)
    assert response.status_code == 200
    assert response.json() == {"content": "## Identity\nJosh"}


def test_get_user_md_missing_file_is_empty(files_home: Path) -> None:
    response = _owner_client().get(USER_MD_PATH)
    assert response.status_code == 200
    assert response.json() == {"content": ""}


def test_put_user_md_writes_atomically(files_home: Path) -> None:
    (files_home / "USER.md").write_text("old", encoding="utf-8")
    response = _owner_client().put(USER_MD_PATH, json={"content": "new profile"})
    assert response.status_code == 200
    assert (files_home / "USER.md").read_text(encoding="utf-8") == "new profile"
    assert response.json() == {"content": "new profile"}


def test_put_user_md_empty_content_clears_profile(files_home: Path) -> None:
    (files_home / "USER.md").write_text("old", encoding="utf-8")
    response = _owner_client().put(USER_MD_PATH, json={"content": ""})
    assert response.status_code == 200
    assert (files_home / "USER.md").read_text(encoding="utf-8") == ""


def test_put_user_md_accepts_exact_decoded_ceiling(files_home: Path) -> None:
    content = "a" * USER_MD_CONTENT_MAX_BYTES
    response = _owner_client().put(USER_MD_PATH, json={"content": content})
    assert response.status_code == 200
    assert (files_home / "USER.md").read_bytes() == content.encode()


def test_put_user_md_refuses_decoded_ceiling_plus_one(files_home: Path) -> None:
    (files_home / "USER.md").write_text("keep", encoding="utf-8")
    response = _owner_client().put(
        USER_MD_PATH, json={"content": "a" * (USER_MD_CONTENT_MAX_BYTES + 1)}
    )
    assert response.status_code == 413
    assert (files_home / "USER.md").read_text(encoding="utf-8") == "keep"


def test_put_user_md_refuses_content_length_above_wire_max(files_home: Path) -> None:
    (files_home / "USER.md").write_text("keep", encoding="utf-8")
    response = _owner_client().put(
        USER_MD_PATH,
        content=b"x",
        headers={
            "content-type": "application/json",
            "content-length": str(USER_MD_WIRE_MAX_BYTES + 1),
        },
    )
    assert response.status_code == 413
    assert (files_home / "USER.md").read_text(encoding="utf-8") == "keep"


def test_put_user_md_refuses_streamed_byte_above_wire_max(files_home: Path) -> None:
    (files_home / "USER.md").write_text("keep", encoding="utf-8")
    app = FastAPI()
    app.include_router(create_hub_files_proxy_router())

    @app.middleware("http")
    async def drop_content_length(request: Request, call_next: Any) -> Response:
        request.scope["headers"] = [
            (key, value)
            for key, value in request.scope["headers"]
            if key != b"content-length"
        ]
        return cast(Response, await call_next(request))

    client = TestClient(app)
    response = client.put(
        USER_MD_PATH,
        content=b"x" * (USER_MD_WIRE_MAX_BYTES + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert (files_home / "USER.md").read_text(encoding="utf-8") == "keep"


def test_owner_user_md_accepts_first_hop(files_home: Path) -> None:
    (files_home / "USER.md").write_text("hub", encoding="utf-8")
    response = _owner_client().get(USER_MD_PATH, headers={FILES_PROXY_HOP_HEADER: "1"})
    assert response.status_code == 200
    assert response.json() == {"content": "hub"}


def test_remote_user_md_is_remote_target(isolated_home: Path) -> None:
    del isolated_home
    _write_remote_bootstrap()
    response = _owner_client().get(USER_MD_PATH, headers={FILES_PROXY_HOP_HEADER: "1"})
    assert response.status_code in {400, 409, 421}
    payload = response.json()
    detail = payload.get("detail", payload)
    if isinstance(detail, dict):
        assert detail.get("error") == "remote_target"
    else:
        assert "remote" in str(detail).lower()


def test_repeated_hop_on_remote_wiki_refuses(isolated_home: Path) -> None:
    from gobby.servers.routes.wiki import create_wiki_router

    del isolated_home
    _write_remote_bootstrap()
    server = create_http_server(config=DaemonConfig())
    app = FastAPI()
    app.include_router(create_wiki_router(server))
    client = TestClient(app)
    response = client.get(
        "/api/wiki/status",
        params={"topic": "research"},
        headers={FILES_PROXY_HOP_HEADER: "1"},
    )
    assert response.status_code in {400, 409, 421}
    assert "hop" in response.text.lower() or "remote" in response.text.lower()
    assert not (Path.home() / "wiki").exists()


def test_remote_wiki_topic_proxies_without_creating_home_wiki(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.servers.routes.wiki import create_wiki_router

    del isolated_home
    _write_remote_bootstrap("http://hub.example.test:7443")
    captured: dict[str, Any] = {}

    async def fake_proxy(request: Request, **kwargs: Any) -> dict[str, Any]:
        captured["path"] = request.url.path
        captured["kwargs"] = kwargs
        return {"ok": True, "proxied": True}

    monkeypatch.setattr("gobby.wiki.owner_dispatch.proxy_owner_request", fake_proxy)
    server = create_http_server(config=DaemonConfig())
    app = FastAPI()
    app.include_router(create_wiki_router(server))
    response = TestClient(app).get("/api/wiki/status", params={"topic": "research"})
    assert response.status_code == 200
    assert response.json()["proxied"] is True
    assert captured["path"] == "/api/wiki/status"
    assert not (Path.home() / "wiki").exists()
    assert not (get_gobby_home() / "personal").exists()


def test_remote_attachment_upload_proxies_and_skips_local_projects(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.servers.routes.chat_attachments import create_chat_attachments_router

    del isolated_home
    _write_remote_bootstrap()
    captured: dict[str, Any] = {}

    async def fake_proxy(request: Request, **kwargs: Any) -> dict[str, Any]:
        captured["path"] = request.url.path
        captured["content_type"] = request.headers.get("content-type")
        captured["kwargs"] = kwargs
        return {"id": "att-1", "ok": True}

    monkeypatch.setattr("gobby.wiki.owner_dispatch.proxy_owner_request", fake_proxy)
    server = create_http_server(config=DaemonConfig())
    app = FastAPI()
    app.include_router(create_chat_attachments_router(server))
    response = TestClient(app).post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 200
    assert captured["path"] == "/api/chat/attachments"
    assert captured["content_type"] is not None
    assert "multipart" in captured["content_type"]
    assert not (get_gobby_home() / "projects").exists()


def test_remote_attachment_download_and_delete_proxy(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.servers.routes.chat_attachments import create_chat_attachments_router

    del isolated_home
    _write_remote_bootstrap()
    calls: list[tuple[str, str]] = []

    async def fake_proxy(request: Request, **kwargs: Any) -> Any:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return Response(content=b"hub-bytes", media_type="text/plain")
        return {"ok": True}

    monkeypatch.setattr("gobby.wiki.owner_dispatch.proxy_owner_request", fake_proxy)
    server = create_http_server(config=DaemonConfig())
    app = FastAPI()
    app.include_router(create_chat_attachments_router(server))
    client = TestClient(app)
    attachment_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    download = client.get(f"/api/chat/attachments/{attachment_id}/content")
    delete = client.delete(f"/api/chat/attachments/{attachment_id}")
    assert download.status_code == 200
    assert download.content == b"hub-bytes"
    assert delete.status_code == 200
    assert ("GET", f"/api/chat/attachments/{attachment_id}/content") in calls
    assert ("DELETE", f"/api/chat/attachments/{attachment_id}") in calls
    assert not (get_gobby_home() / "projects").exists()


@pytest.mark.asyncio
async def test_daemon_client_origin_join_and_hop(isolated_home: Path) -> None:
    (isolated_home / "local_cli_token").write_text("token\n", encoding="utf-8")
    client = DaemonClient.from_url("http://hub.example.test:7443")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://hub.example.test:7443/api/files/user-md"
        assert request.headers.get(FILES_PROXY_HOP_HEADER) == "1"
        assert request.headers.get("authorization")
        return httpx.Response(200, json={"content": "ok"})

    with _patch_async_client(httpx.MockTransport(handler)):
        response = await client.request_raw("GET", USER_MD_PATH, hop=True)
    assert response.status_code == 200
    assert response.json() == {"content": "ok"}


@pytest.mark.asyncio
async def test_daemon_client_timeout_and_auth_are_typed(isolated_home: Path) -> None:
    del isolated_home
    client = DaemonClient.from_url("http://hub.example.test:7443")

    def timeout_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("stalled")

    with _patch_async_client(httpx.MockTransport(timeout_handler)):
        with pytest.raises(DaemonTimeoutError):
            await client.request_raw("GET", USER_MD_PATH, hop=True)

    def auth_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "auth"})

    with _patch_async_client(httpx.MockTransport(auth_handler)):
        with pytest.raises(DaemonAuthenticationError):
            await client.request_raw("GET", USER_MD_PATH, hop=True)


@pytest.mark.asyncio
async def test_async_request_yields_and_cancellation_closes_client(
    isolated_home: Path,
) -> None:
    del isolated_home
    client = DaemonClient.from_url("http://hub.example.test:7443")
    closed = {"done": False}
    started = asyncio.Event()

    class SlowClient(httpx.AsyncClient):
        async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            return httpx.Response(200)

        async def aclose(self) -> None:
            closed["done"] = True
            await super().aclose()

    with patch.object(httpx, "AsyncClient", SlowClient):
        task = asyncio.create_task(client.request_raw("GET", USER_MD_PATH, hop=True))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed["done"] is True


def test_remote_file_bearing_uses_request_stream(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.servers.routes.chat_attachments import create_chat_attachments_router

    del isolated_home
    _write_remote_bootstrap()
    streamed = {"used": False}

    async def fake_proxy(request: Request, **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("stream_body") is True
        agen = request.stream()
        first = await agen.__anext__()
        assert first
        streamed["used"] = True
        return {"ok": True}

    monkeypatch.setattr("gobby.wiki.owner_dispatch.proxy_owner_request", fake_proxy)
    server = create_http_server(config=DaemonConfig())
    app = FastAPI()
    app.include_router(create_chat_attachments_router(server))
    response = TestClient(app).post(
        "/api/chat/attachments",
        files={"file": ("note.txt", b"hello-stream", "text/plain")},
    )
    assert response.status_code == 200
    assert streamed["used"] is True


def test_remote_attach_sends_bytes_not_path(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.servers.routes.wiki import create_wiki_router

    del isolated_home
    _write_remote_bootstrap()
    captured: dict[str, Any] = {}

    async def fake_proxy(request: Request, **kwargs: Any) -> dict[str, Any]:
        captured["path"] = request.url.path
        captured["stream_body"] = kwargs.get("stream_body")
        captured["json_body"] = kwargs.get("json_body")
        return {"ok": True, "command": "attach"}

    monkeypatch.setattr("gobby.wiki.owner_dispatch.proxy_owner_request", fake_proxy)
    server = create_http_server(config=DaemonConfig())
    app = FastAPI()
    app.include_router(create_wiki_router(server))
    response = TestClient(app).post(
        "/api/wiki/attach",
        params={"topic": "research"},
        files={"file": ("note.md", b"# Note", "text/markdown")},
    )
    assert response.status_code == 200
    assert captured["path"] == "/api/wiki/attach"
    assert captured["stream_body"] is True
    if captured["json_body"] is not None:
        assert "path" not in json.dumps(captured["json_body"])
    assert not (Path.home() / "wiki").exists()


@pytest.mark.asyncio
async def test_attachment_and_wiki_forward_conditional_headers(
    isolated_home: Path,
) -> None:
    del isolated_home
    client = DaemonClient.from_url("http://hub.example.test:7443")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        for name in ("range", "if-none-match", "if-modified-since"):
            value = request.headers.get(name)
            if value:
                seen[name] = value
        return httpx.Response(
            304,
            headers={
                "etag": '"abc"',
                "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT",
                "accept-ranges": "bytes",
            },
        )

    transport = httpx.MockTransport(handler)
    with _patch_async_client(transport):
        response = await client.request_raw(
            "GET",
            "/api/chat/attachments/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/content",
            hop=True,
            headers={
                "Range": "bytes=0-3",
                "If-None-Match": '"abc"',
                "If-Modified-Since": "Wed, 21 Oct 2015 07:28:00 GMT",
            },
            accept_statuses=(200, 206, 304),
        )
    assert response.status_code == 304
    assert seen["range"] == "bytes=0-3"
    assert seen["if-none-match"] == '"abc"'
    with _patch_async_client(transport):
        with pytest.raises(DaemonStatusError):
            await client.request_raw("GET", USER_MD_PATH, hop=True, accept_statuses=(200,))


def test_remote_conversation_delete_proxies_owner_cleanup(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.servers.routes.chat import create_chat_router

    del isolated_home
    _write_remote_bootstrap()
    captured: dict[str, Any] = {}

    async def fake_proxy(request: Request, **kwargs: Any) -> dict[str, Any]:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return {"deleted": 2}

    monkeypatch.setattr("gobby.wiki.owner_dispatch.proxy_owner_request", fake_proxy)
    server = create_http_server(config=DaemonConfig())
    server.session_manager = SimpleNamespace(db=object())
    app = FastAPI()
    app.include_router(create_chat_router(server))
    response = TestClient(app).delete("/api/chat/conv-1/messages")
    assert response.status_code == 200
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/chat/conv-1/messages"


@pytest.mark.asyncio
async def test_sync_sessions_container_refuses_special_and_missing_wiki(
    isolated_home: Path, tmp_path: Path
) -> None:
    from gobby.wiki.sync_container import SyncContainerError, build_sync_container

    del isolated_home
    archive = tmp_path / "archives"
    wiki = tmp_path / "wiki"
    archive.mkdir()
    wiki.mkdir()
    (archive / "ok.jsonl").write_text("{}", encoding="utf-8")
    os_module = __import__("os")
    if not hasattr(os_module, "mkfifo"):
        pytest.skip("mkfifo unavailable")
    os_module.mkfifo(archive / "fifo")
    with pytest.raises(SyncContainerError, match="special"):
        build_sync_container(archive_dir=archive, wiki_dir=wiki)
    (archive / "fifo").unlink()
    with pytest.raises(SyncContainerError, match="wiki_dir"):
        build_sync_container(archive_dir=archive, wiki_dir=tmp_path / "missing-wiki")


def test_remote_upload_then_bind_uses_hub_identity(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.servers.websocket.chat_attachments import _bind_attachments_sync

    del isolated_home
    _write_remote_bootstrap()
    record = SimpleNamespace(
        id="att-hub",
        project_id="proj",
        filename="a.txt",
        mime_type="text/plain",
        size_bytes=4,
        published=True,
    )

    def fake_get(_db: object, ids: list[str], **_kwargs: Any) -> list[Any]:
        assert ids == ["att-hub"]
        return [record]

    bound: dict[str, list[str]] = {"ids": []}

    def fake_bind(_db: object, ids: list[str], **_kwargs: Any) -> list[Any]:
        bound["ids"] = ids
        return [record]

    monkeypatch.setattr("gobby.storage.chat_attachments.get_attachments_by_ids", fake_get)
    monkeypatch.setattr("gobby.storage.chat_attachments.bind_attachments", fake_bind)
    owner = SimpleNamespace(session_manager=SimpleNamespace(db=object()))
    result = _bind_attachments_sync(
        owner,
        ["att-hub"],
        max_file_bytes=1000,
        max_files_per_message=4,
        max_total_bytes=4000,
        conversation_id="c1",
        message_id="m1",
        target_session_id=None,
    )
    assert [item.id for item in result] == ["att-hub"]
    assert bound["ids"] == ["att-hub"]


def test_wiki_and_scheduled_modules_stay_under_line_ceiling() -> None:
    repo = Path(__file__).resolve().parents[3]
    wiki = (repo / "src/gobby/servers/routes/wiki.py").read_text(encoding="utf-8")
    jobs = (repo / "src/gobby/wiki/scheduled_jobs.py").read_text(encoding="utf-8")
    assert wiki.count("\n") < 1000
    assert jobs.count("\n") < 1000
