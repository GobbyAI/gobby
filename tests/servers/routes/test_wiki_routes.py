from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.gwiki_gateway import (
    GENERATION_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS,
    GwikiCommandError,
    GwikiGateway,
)
from gobby.servers.routes import wiki as wiki_routes
from gobby.servers.routes.wiki import _stage_upload, create_wiki_router
from gobby.storage.projects import (
    PERSONAL_PROJECT_ID,
    LocalProjectManager,
    ensure_personal_project,
)
from tests.servers.conftest import create_http_server

pytestmark = pytest.mark.unit


class FakeGateway:
    instances: list[FakeGateway] = []
    next_error: Exception | None = None
    next_result: dict[str, Any] | None = None

    def __init__(
        self,
        *,
        binary: str | None = None,
        project_root: str | Path | None = None,
        topic: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.binary = binary
        self.project = str(project_root) if project_root is not None else None
        self.topic = topic
        self.timeout_seconds = timeout_seconds
        self.calls: list[tuple[str, Any]] = []
        self.index_calls = 0
        FakeGateway.instances.append(self)

    async def status(self) -> dict[str, Any]:
        self.calls.append(("status", None))
        return self._result("status")

    async def index(self) -> dict[str, Any]:
        self.index_calls += 1
        self.calls.append(("index", None))
        return self._result("index", payload={"indexed": {"documents": 1}})

    async def search(self, query: str, *, limit: int | None = None) -> dict[str, Any]:
        self.calls.append(("search", {"query": query, "limit": limit}))
        return self._result("search", payload={"query": query, "limit": limit})

    async def read(
        self,
        *,
        path: str | Path | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("read", {"path": str(path) if path is not None else None, "title": title})
        )
        return self._result(
            "read",
            payload={
                "command": "read",
                "path": str(path) if path is not None else None,
                "title": title,
                "content": "# Page\n\nBody",
                "status": "found",
            },
        )

    async def graph(self, *, include: str = "all") -> dict[str, Any]:
        self.calls.append(("graph", {"include": include}))
        return self._result(
            "graph", payload={"include": include, "graph": {"documents": [], "links": []}}
        )

    async def pages(self, *, prefix: str | None = None) -> dict[str, Any]:
        self.calls.append(("pages", {"prefix": prefix}))
        return self._result("pages", payload={"prefix": prefix, "pages": [], "outputs": []})

    async def backlinks(self, target: str) -> dict[str, Any]:
        self.calls.append(("backlinks", target))
        return self._result("backlinks", payload={"target": target, "links": []})

    async def write_page(
        self,
        *,
        path: str,
        content: str,
        mode: str = "upsert",
        expected_hash: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "write_page",
                {"path": path, "content": content, "mode": mode, "expected_hash": expected_hash},
            )
        )
        if FakeGateway.next_error is not None:
            error, FakeGateway.next_error = FakeGateway.next_error, None
            raise error
        return self._result(
            "page-write",
            payload={
                "path": path,
                "created": True,
                "bytes": len(content.encode()),
                "content_hash": "hash-1",
                "changed_paths": [path],
            },
        )

    async def delete_page(self, *, path: str) -> dict[str, Any]:
        self.calls.append(("delete_page", {"path": path}))
        if FakeGateway.next_error is not None:
            error, FakeGateway.next_error = FakeGateway.next_error, None
            raise error
        return self._result("page-delete", payload={"path": path, "changed_paths": [path]})

    async def ingest_file(self, path: str | Path) -> dict[str, Any]:
        staged = Path(path)
        self.calls.append(("ingest_file", {"path": str(staged), "exists": staged.exists()}))
        return self._result(
            "ingest_file",
            payload={"command": "ingest-file", "changed_paths": [str(staged)]},
        )

    async def ingest_url(self, urls: list[str]) -> dict[str, Any]:
        self.calls.append(("ingest_url", list(urls)))
        if FakeGateway.next_error is not None:
            raise FakeGateway.next_error
        payload = FakeGateway.next_result or {
            "command": "ingest-url",
            "status": "partial",
            "accepted": [{"requested_url": urls[0], "raw_path": "raw/url.md"}],
            "failed": [{"url": urls[-1], "code": "blocked", "message": "blocked"}],
            "indexed": {"documents": 1, "chunks": 2, "links": 3, "sources": 1, "ingestions": 1},
        }
        return {"ok": True, "command": "ingest_url", "payload": payload, "stderr": "warning"}

    async def collect(self, query: str | None = None) -> dict[str, Any]:
        self.calls.append(("collect", query))
        return self._result(
            "collect", payload={"command": "collect", "changed_paths": ["raw/a.md"]}
        )

    async def compile(
        self,
        topic: str | None = None,
        *,
        kind: str | None = None,
        sources: list[str] | None = None,
        outline: list[str] | None = None,
        target: str | Path | None = None,
        write_intent: bool = False,
        ai: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "compile",
                {
                    "topic": topic,
                    "kind": kind,
                    "sources": sources,
                    "outline": outline,
                    "target": str(target) if target is not None else None,
                    "write_intent": write_intent,
                    "ai": ai,
                },
            )
        )
        return self._result(
            "compile", payload={"command": "compile", "changed_paths": ["wiki/a.md"]}
        )

    async def audit(self) -> dict[str, Any]:
        self.calls.append(("audit", None))
        return self._result("audit")

    async def health(self) -> dict[str, Any]:
        self.calls.append(("health", None))
        return self._result("health", payload={"status": "healthy"})

    async def sources(self) -> dict[str, Any]:
        self.calls.append(("sources", None))
        return self._result("sources", payload={"sources": [{"id": "src-1"}]})

    async def remove_source(
        self,
        source_id: str,
        *,
        dry_run: bool,
        yes: bool,
        keep_asset: bool,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "remove_source",
                {"id": source_id, "dry_run": dry_run, "yes": yes, "keep_asset": keep_asset},
            )
        )
        if FakeGateway.next_error is not None:
            raise FakeGateway.next_error
        return self._result(
            "remove_source",
            payload={
                "command": "remove-source",
                "source": {"id": source_id},
                "dry_run": dry_run,
                "index_status": {"index_required": yes},
            },
        )

    def _result(self, command: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"command": command, **(payload or {})}
        return {"ok": True, "command": command, "payload": payload, "stderr": ""}


class RecordingCoordinator:
    instances: list[RecordingCoordinator] = []

    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self.results: list[dict[str, Any]] = []
        RecordingCoordinator.instances.append(self)

    async def handle_write_result(self, result: dict[str, Any]) -> dict[str, Any]:
        self.results.append(result)
        response = dict(result)
        response["index_handoff"] = {"status": "recorded"}
        return response


@pytest.fixture(autouse=True)
def reset_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    import gobby.servers.routes.wiki as wiki_routes

    FakeGateway.instances = []
    FakeGateway.next_error = None
    FakeGateway.next_result = None
    RecordingCoordinator.instances = []
    monkeypatch.setattr(wiki_routes, "GwikiGateway", FakeGateway)


@pytest.fixture
def client(temp_db: Any, tmp_path: Path) -> TestClient:
    project = LocalProjectManager(temp_db).create(name="wiki-route-client", repo_path=str(tmp_path))
    app = FastAPI()
    server = SimpleNamespace(
        services=SimpleNamespace(config=SimpleNamespace(), database=temp_db, project_id=project.id)
    )
    app.include_router(create_wiki_router(server))
    return TestClient(app)


def test_status_search_read_and_gateway_scope(client: TestClient) -> None:
    invalid_scope = client.get("/api/wiki/status", params={"project": "p", "topic": "t"})
    assert invalid_scope.status_code == 400
    assert invalid_scope.json()["detail"] == "Provide project or topic scope, not both"

    search = client.get("/api/wiki/search", params={"query": "hooks", "limit": 3, "topic": "t"})
    assert search.status_code == 200
    assert search.json()["payload"] == {"command": "search", "query": "hooks", "limit": 3}
    assert FakeGateway.instances[-1].project is None
    assert FakeGateway.instances[-1].topic == "t"

    no_selector = client.get("/api/wiki/read")
    both_selectors = client.get("/api/wiki/read", params={"path": "a.md", "title": "A"})
    assert no_selector.status_code == 400
    assert both_selectors.status_code == 400

    retired_ask = client.get("/api/wiki/ask", params={"q": "hooks?"})
    assert retired_ask.status_code == 404

    read = client.get("/api/wiki/read", params={"title": "A"})
    assert read.status_code == 200
    assert read.json()["payload"]["content"] == "# Page\n\nBody"
    assert FakeGateway.instances[-1].calls == [("read", {"path": None, "title": "A"})]


def test_project_scope_resolves_to_repo_path(temp_db: Any, tmp_path: Path) -> None:
    project = LocalProjectManager(temp_db).create(name="wiki-route", repo_path=str(tmp_path))
    app = FastAPI()
    server = SimpleNamespace(
        services=SimpleNamespace(
            config=SimpleNamespace(),
            database=temp_db,
            project_id=project.id,
        )
    )
    app.include_router(create_wiki_router(server))
    client = TestClient(app)

    response = client.get("/api/wiki/search", params={"query": "hooks", "project": project.id})

    assert response.status_code == 200
    assert FakeGateway.instances[-1].project == str(tmp_path.resolve())


def test_personal_scope_routes_resolve_uninitialized_workspace(
    temp_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Personal topic-less scope uses files_home/_personal on the owner."""
    gobby_home = tmp_path / "gobby-home"
    files_home = tmp_path / "files_home"
    gobby_home.mkdir()
    files_home.mkdir()
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    bootstrap = gobby_home / "bootstrap.yaml"
    bootstrap.write_text(
        f"datastore_mode: local\nfiles_home: {files_home}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    ensure_personal_project(temp_db)

    app = FastAPI()
    server = SimpleNamespace(
        services=SimpleNamespace(config=SimpleNamespace(), database=temp_db, project_id=None)
    )
    app.include_router(create_wiki_router(cast(Any, server)))
    client = TestClient(app)

    personal_root = (files_home / "_personal").resolve()
    for route in ("/api/wiki/status", "/api/wiki/pages", "/api/wiki/graph"):
        response = client.get(route, params={"project": PERSONAL_PROJECT_ID})
        assert response.status_code == 200, route
        assert response.json()["ok"] is True, route
        assert FakeGateway.instances[-1].project == str(personal_root), route


def test_backlinks_health_and_sources_passthrough(client: TestClient) -> None:
    assert (
        client.get("/api/wiki/backlinks", params={"target": "A"}).json()["payload"]["target"] == "A"
    )
    assert client.get("/api/wiki/health").json()["payload"]["status"] == "healthy"
    assert FakeGateway.instances[-1].timeout_seconds == INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS
    assert client.get("/api/wiki/sources").json()["payload"]["sources"] == [{"id": "src-1"}]


def test_graph_route_include_validation(client: TestClient) -> None:
    filtered = client.get("/api/wiki/graph", params={"include": "knowledge", "topic": "t"})
    assert filtered.status_code == 200
    body = filtered.json()
    assert body["ok"] is True
    assert body["payload"]["include"] == "knowledge"
    gateway = FakeGateway.instances[-1]
    assert gateway.calls == [("graph", {"include": "knowledge"})]
    assert gateway.topic == "t"

    default = client.get("/api/wiki/graph")
    assert default.status_code == 200
    assert FakeGateway.instances[-1].calls == [("graph", {"include": "all"})]

    gateways_before = len(FakeGateway.instances)
    invalid = client.get("/api/wiki/graph", params={"include": "everything"})
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "include must be one of all, code, knowledge"
    assert len(FakeGateway.instances) == gateways_before


def test_pages_route_passes_prefix_and_scope(client: TestClient) -> None:
    scoped = client.get("/api/wiki/pages", params={"prefix": "code/", "topic": "t"})
    assert scoped.status_code == 200
    body = scoped.json()
    assert body["ok"] is True
    assert body["payload"]["prefix"] == "code/"
    gateway = FakeGateway.instances[-1]
    assert gateway.calls == [("pages", {"prefix": "code/"})]
    assert gateway.topic == "t"

    unfiltered = client.get("/api/wiki/pages")
    assert unfiltered.status_code == 200
    assert FakeGateway.instances[-1].calls == [("pages", {"prefix": None})]


def test_gzip_enabled(temp_db: Any) -> None:
    server = create_http_server(config=DaemonConfig(), database=temp_db)
    app_client = TestClient(server.app)
    query = "x" * 4096

    compressed = app_client.get(
        "/api/wiki/search",
        params={"q": query},
        headers={"Accept-Encoding": "gzip"},
    )
    assert compressed.status_code == 200
    assert compressed.headers.get("content-encoding") == "gzip"
    assert compressed.json()["payload"]["query"] == query

    identity = app_client.get(
        "/api/wiki/search",
        params={"q": query},
        headers={"Accept-Encoding": "identity"},
    )
    assert identity.status_code == 200
    assert "content-encoding" not in identity.headers


def test_gateway_error_mapping_preserves_cli_stderr(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_search(
        self: FakeGateway, query: str, *, limit: int | None = None
    ) -> dict[str, Any]:
        raise GwikiCommandError(
            command="search",
            argv=("gwiki", "search", query),
            returncode=2,
            stderr="scope does not exist",
            payload={"code": "missing_scope", "hint": "run setup"},
        )

    monkeypatch.setattr(FakeGateway, "search", fail_search)

    response = client.get("/api/wiki/search", params={"query": "hooks"})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["stderr"] == "scope does not exist"
    assert detail["payload"] == {"code": "missing_scope", "hint": "run setup"}


def test_source_routes_contract(client: TestClient) -> None:
    missing_id = client.post("/api/wiki/remove-source", json={})
    dry_run_and_yes = client.post(
        "/api/wiki/remove-source",
        json={"id": "src-1", "dry_run": True, "yes": True},
    )
    preview = client.post("/api/wiki/remove-source", json={"id": "src-1", "keep_asset": True})
    confirmed = client.post("/api/wiki/remove-source", json={"id": "src-1", "yes": True})

    assert missing_id.status_code == 400
    assert dry_run_and_yes.status_code == 400
    assert preview.status_code == 200
    assert confirmed.status_code == 200
    assert FakeGateway.instances[-2].calls == [
        ("remove_source", {"id": "src-1", "dry_run": True, "yes": False, "keep_asset": True})
    ]
    assert FakeGateway.instances[-1].calls[0] == (
        "remove_source",
        {"id": "src-1", "dry_run": False, "yes": True, "keep_asset": False},
    )


def test_remove_source_error_mapping(client: TestClient) -> None:
    FakeGateway.next_error = GwikiCommandError(
        command="remove_source",
        argv=("gwiki", "remove-source", "--id", "src-1"),
        returncode=3,
        stderr="cannot remove source",
        payload={"follow_up": ["audit_recommended"], "source": {"id": "src-1"}},
    )

    response = client.post("/api/wiki/remove-source", json={"id": "src-1", "yes": True})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["stderr"] == "cannot remove source"
    assert detail["payload"]["follow_up"] == ["audit_recommended"]


def test_ingest_url_batch_routes_to_gateway(client: TestClient) -> None:
    response = client.post(
        "/api/wiki/ingest",
        json={"urls": ["https://example.test/a", "https://example.test/b"]},
    )

    assert response.status_code == 200
    assert response.json()["payload"]["accepted"][0]["requested_url"] == "https://example.test/a"
    assert FakeGateway.instances[-1].calls == [
        ("ingest_url", ["https://example.test/a", "https://example.test/b"])
    ]


def test_ingest_mixed_urls_and_paths_routes_to_gateway(client: TestClient) -> None:
    response = client.post(
        "/api/wiki/ingest",
        json={"path": "docs/path with spaces.md", "urls": ["https://example.test/a"]},
    )

    assert response.status_code == 200
    project_root = Path(cast(str, FakeGateway.instances[-1].project))
    resolved_path = str(project_root / "docs/path with spaces.md")
    body = response.json()
    assert body["payload"]["changed_paths"] == [resolved_path]
    assert body["payload"]["results"][0]["payload"]["accepted"][0]["requested_url"] == (
        "https://example.test/a"
    )
    assert FakeGateway.instances[-1].calls == [
        ("ingest_url", ["https://example.test/a"]),
        (
            "ingest_file",
            {"path": resolved_path, "exists": False},
        ),
        ("index", None),
    ]


def test_ingest_mixed_urls_and_multiple_paths_flattens_results(client: TestClient) -> None:
    response = client.post(
        "/api/wiki/ingest",
        json={
            "paths": ["docs/a.md", "docs/b.md"],
            "urls": ["https://example.test/a"],
        },
    )

    assert response.status_code == 200
    project_root = Path(cast(str, FakeGateway.instances[-1].project))
    resolved_paths = [str(project_root / "docs/a.md"), str(project_root / "docs/b.md")]
    body = response.json()
    assert body["payload"]["changed_paths"] == resolved_paths
    assert [result["command"] for result in body["payload"]["results"]] == [
        "ingest_url",
        "ingest_file",
        "ingest_file",
    ]
    assert all("results" not in result["payload"] for result in body["payload"]["results"])
    assert FakeGateway.instances[-1].calls == [
        ("ingest_url", ["https://example.test/a"]),
        ("ingest_file", {"path": resolved_paths[0], "exists": False}),
        ("ingest_file", {"path": resolved_paths[1], "exists": False}),
        ("index", None),
    ]


def test_ingest_mixed_ignores_invalid_gateway_changed_paths(client: TestClient) -> None:
    FakeGateway.next_result = {
        "command": "ingest-url",
        "changed_paths": ["raw/url.md", "", None, 42, "raw/url.md"],
    }

    response = client.post(
        "/api/wiki/ingest",
        json={"path": "docs/path with spaces.md", "urls": ["https://example.test/a"]},
    )

    assert response.status_code == 200
    project_root = Path(cast(str, FakeGateway.instances[-1].project))
    body = response.json()
    assert body["payload"]["changed_paths"] == [
        "raw/url.md",
        str(project_root / "docs/path with spaces.md"),
    ]


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.md"])
def test_ingest_rejects_paths_outside_project(client: TestClient, path: str) -> None:
    response = client.post("/api/wiki/ingest", json={"path": path})

    assert response.status_code == 403
    assert response.json()["detail"] == "Ingest path must stay inside project"
    assert FakeGateway.instances == []


def test_ingest_url_batch_passthrough_and_indexing(client: TestClient) -> None:
    response = client.post(
        "/api/wiki/ingest",
        json={"urls": ["https://example.test/a", "https://example.test/b"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["payload"]["status"] == "partial"
    assert body["payload"]["indexed"] == {
        "documents": 1,
        "chunks": 2,
        "links": 3,
        "sources": 1,
        "ingestions": 1,
    }
    assert body["stderr"] == "warning"
    assert body["index_handoff"] == {"status": "skipped", "reason": "cli_indexed_batch"}
    assert FakeGateway.instances[-1].index_calls == 0

    FakeGateway.next_error = GwikiCommandError(
        command="ingest_url",
        argv=("gwiki", "ingest-url", "https://example.test/a"),
        returncode=4,
        stderr="all urls failed",
        payload={"command": "ingest-url", "status": "failed", "failed": [{"url": "https://x"}]},
    )
    failed = client.post("/api/wiki/ingest", json={"urls": ["https://example.test/a"]})

    assert failed.status_code == 502
    assert failed.json()["detail"]["stderr"] == "all urls failed"
    assert failed.json()["detail"]["payload"]["status"] == "failed"


def test_write_routes_trigger_index(client: TestClient) -> None:
    response = client.post("/api/wiki/collect", json={"query": "release notes"})

    assert response.status_code == 200
    assert response.json()["index_handoff"]["status"] == "indexed"
    assert FakeGateway.instances[-1].index_calls == 1


def test_write_awaits_reindex(client: TestClient) -> None:
    response = client.post(
        "/api/wiki/write",
        json={"path": "knowledge/notes/demo.md", "content": "# Demo\n\nBody\n"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["payload"]["path"] == "knowledge/notes/demo.md"
    assert body["index_handoff"]["status"] == "indexed"
    assert body["index_handoff"]["changed_paths"] == ["knowledge/notes/demo.md"]
    gateway = FakeGateway.instances[-1]
    assert gateway.calls == [
        (
            "write_page",
            {
                "path": "knowledge/notes/demo.md",
                "content": "# Demo\n\nBody\n",
                "mode": "upsert",
                "expected_hash": None,
            },
        ),
        ("index", None),
    ]

    deleted = client.post("/api/wiki/delete", json={"path": "knowledge/notes/demo.md"})

    assert deleted.status_code == 200
    assert deleted.json()["index_handoff"]["status"] == "indexed"
    assert FakeGateway.instances[-1].calls == [
        ("delete_page", {"path": "knowledge/notes/demo.md"}),
        ("index", None),
    ]


def test_write_delete_error_mapping(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_path = client.post("/api/wiki/write", json={"content": "x"})
    assert missing_path.status_code == 400
    non_string_content = client.post("/api/wiki/write", json={"path": "knowledge/a.md"})
    assert non_string_content.status_code == 400
    bad_mode = client.post(
        "/api/wiki/write",
        json={"path": "knowledge/a.md", "content": "x", "mode": "replace"},
    )
    assert bad_mode.status_code == 400

    FakeGateway.next_error = GwikiCommandError(
        command="write_page",
        argv=("gwiki", "page", "write", "--path", "knowledge/a.md", "--mode", "create"),
        returncode=2,
        stderr="wiki page `knowledge/a.md` already exists (already_exists)",
        payload={"code": "already_exists", "message": "wiki page `knowledge/a.md` already exists"},
    )
    conflict = client.post(
        "/api/wiki/write",
        json={"path": "knowledge/a.md", "content": "x", "mode": "create"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["payload"]["code"] == "already_exists"

    FakeGateway.next_error = GwikiCommandError(
        command="delete_page",
        argv=("gwiki", "page", "delete", "--path", "knowledge/missing.md"),
        returncode=2,
        stderr="wiki page `knowledge/missing.md` not found (not_found)",
        payload={"code": "not_found", "message": "wiki page `knowledge/missing.md` not found"},
    )
    missing = client.post("/api/wiki/delete", json={"path": "knowledge/missing.md"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["payload"]["code"] == "not_found"

    # Stale expected hash runs a real gateway subclass so the assertion proves
    # the hash reaches the gwiki argv through the reindex-backed write path.
    recorded_argv: list[tuple[str, ...]] = []

    class RecordingGateway(GwikiGateway):
        async def _resolve_binary(self) -> str:
            return "/bin/gwiki"

        async def _run_command(
            self,
            command_name: str,
            argv: Any,
            *,
            stdin_data: bytes | None = None,
        ) -> tuple[bytes, str] | dict[str, Any]:
            recorded_argv.append(tuple(argv))
            raise GwikiCommandError(
                command=command_name,
                argv=argv,
                returncode=2,
                stderr=(
                    "expected content hash deadbeef, found cafef00d for wiki page "
                    "`knowledge/a.md` (precondition_failed)"
                ),
                payload={"code": "precondition_failed", "message": "content hash mismatch"},
            )

    monkeypatch.setattr(wiki_routes, "GwikiGateway", RecordingGateway)
    stale = client.post(
        "/api/wiki/write",
        json={"path": "knowledge/a.md", "content": "new body", "expected_hash": "deadbeef"},
    )

    assert stale.status_code == 412
    assert stale.json()["detail"]["payload"]["code"] == "precondition_failed"
    assert len(recorded_argv) == 1
    argv = recorded_argv[0]
    assert argv[1:3] == ("page", "write")
    hash_flag = argv.index("--expected-hash")
    assert argv[hash_flag + 1] == "deadbeef"


def test_research_route_is_removed(client: TestClient) -> None:
    response = client.post("/api/wiki/research", json={"query": "anything"})

    assert response.status_code in (404, 405)


def test_compile_route_passes_full_param_surface(client: TestClient) -> None:
    response = client.post(
        "/api/wiki/compile",
        json={
            "compile_topic": "Hooks Overview",
            "kind": "Topic",
            "sources": ["src-1", "src-2"],
            "outline": ["Intro"],
            "target": "knowledge/topics/hooks.md",
            "write_intent": True,
            "ai": "direct",
        },
    )

    assert response.status_code == 200
    assert FakeGateway.instances[-1].calls[0] == (
        "compile",
        {
            "topic": "Hooks Overview",
            "kind": "topic",
            "sources": ["src-1", "src-2"],
            "outline": ["Intro"],
            "target": "knowledge/topics/hooks.md",
            "write_intent": True,
            "ai": "direct",
        },
    )


def test_compile_route_rejects_unknown_kind_and_ai(client: TestClient) -> None:
    bad_kind = client.post("/api/wiki/compile", json={"kind": "article"})
    assert bad_kind.status_code == 400
    assert bad_kind.json()["detail"] == "kind must be one of concept, source, topic"

    bad_ai = client.post("/api/wiki/compile", json={"ai": "cloud"})
    assert bad_ai.status_code == 400
    assert bad_ai.json()["detail"] == "ai must be one of auto, daemon, direct, off"


def test_compile_route_uses_generation_gateway_timeout(client: TestClient) -> None:
    response = client.post("/api/wiki/compile", json={"ai": "daemon"})

    assert response.status_code == 200
    assert FakeGateway.instances[-1].timeout_seconds == GENERATION_GWIKI_TIMEOUT_SECONDS


def test_write_routes_delegate_to_coordinator(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.servers.routes.wiki as wiki_routes

    monkeypatch.setattr(wiki_routes, "WikiUpdateCoordinator", RecordingCoordinator)

    endpoints = [
        ("post", "/api/wiki/attach", {"files": {"file": ("note.md", b"# Note", "text/markdown")}}),
        ("post", "/api/wiki/ingest", {"json": {"path": "notes/a.md"}}),
        ("post", "/api/wiki/collect", {"json": {"query": "hooks"}}),
        ("post", "/api/wiki/compile", {"json": {"target": "out.md"}}),
        ("post", "/api/wiki/remove-source", {"json": {"id": "src-1", "yes": True}}),
    ]

    for method, path, kwargs in endpoints:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 200
        assert response.json()["index_handoff"] == {"status": "recorded"}

    handled_commands = [item["payload"]["command"] for item in _handled_results()]
    assert handled_commands == [
        "ingest-file",
        "ingest-file",
        "collect",
        "compile",
        "remove-source",
    ]


def test_wiki_router_registered_in_app() -> None:
    server = create_http_server(config=DaemonConfig())

    route_paths = {getattr(route, "path", "") for route in server.app.routes}

    assert "/api/wiki/status" in route_paths
    assert "/api/wiki/remove-source" in route_paths
    assert "/api/wiki/trust" in route_paths
    assert "/api/wiki/refresh" in route_paths
    assert "/api/wiki/export" in route_paths
    assert "/api/wiki/graph-artifacts" in route_paths
    assert "/api/wiki/sync-sessions" in route_paths
    assert "/api/wiki/upkeep" in route_paths
    assert "/api/wiki/librarian" in route_paths
    assert "/api/wiki/recap" in route_paths
    assert "/api/wiki/prune" in route_paths


def _handled_results() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for coordinator in RecordingCoordinator.instances:
        results.extend(coordinator.results)
    return results


@pytest.mark.asyncio
async def test_stage_upload_unlinks_temp_file_on_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_paths: list[Path] = []
    original_named_temporary_file = tempfile.NamedTemporaryFile

    def named_temporary_file(*args: Any, **kwargs: Any) -> Any:
        kwargs["dir"] = tmp_path
        staged = original_named_temporary_file(*args, **kwargs)
        created_paths.append(Path(staged.name))
        return staged

    class FailingUpload:
        filename = "note.md"

        def __init__(self) -> None:
            self.reads = 0

        async def read(self, _size: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise RuntimeError("upload read failed")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", named_temporary_file)

    with pytest.raises(RuntimeError, match="upload read failed"):
        await _stage_upload(cast(UploadFile, FailingUpload()))

    assert created_paths
    assert created_paths[0].exists() is False


@pytest.mark.asyncio
async def test_stage_upload_checks_disk_and_accepts_exact_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[tuple[Path, int, str]] = []
    original_named_temporary_file = tempfile.NamedTemporaryFile

    def named_temporary_file(*args: Any, **kwargs: Any) -> Any:
        kwargs["dir"] = tmp_path
        return original_named_temporary_file(*args, **kwargs)

    def check_disk(directory: Path, incoming_bytes: int, *, label: str) -> None:
        checked.append((directory, incoming_bytes, label))

    upload = UploadFile(filename="note.md", file=cast(BinaryIO, tempfile.SpooledTemporaryFile()))
    upload.file.write(b"1234")
    upload.file.seek(0)
    upload.size = 4
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", named_temporary_file)
    monkeypatch.setattr(wiki_routes, "ensure_disk_space", check_disk)

    staged_path = await _stage_upload(upload, max_bytes=4)
    try:
        assert staged_path.read_bytes() == b"1234"
        assert checked == [(tmp_path, 4, "Wiki upload")]
    finally:
        staged_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_stage_upload_rejects_oversize_and_unlinks_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_paths: list[Path] = []
    original_named_temporary_file = tempfile.NamedTemporaryFile

    def named_temporary_file(*args: Any, **kwargs: Any) -> Any:
        kwargs["dir"] = tmp_path
        staged = original_named_temporary_file(*args, **kwargs)
        created_paths.append(Path(staged.name))
        return staged

    upload = UploadFile(filename="note.md", file=cast(BinaryIO, tempfile.SpooledTemporaryFile()))
    upload.file.write(b"12345")
    upload.file.seek(0)
    upload.size = 5
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", named_temporary_file)
    monkeypatch.setattr(wiki_routes, "ensure_disk_space", MagicMock())

    with pytest.raises(HTTPException) as exc_info:
        await _stage_upload(upload, max_bytes=4)

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "Wiki upload exceeds 4 byte limit"
    assert created_paths
    assert created_paths[0].exists() is False
