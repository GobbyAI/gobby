from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from gobby.config.app import DaemonConfig
from gobby.gwiki_gateway import GwikiCommandError
from gobby.servers.routes.wiki import create_wiki_router
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
        project: str | Path | None = None,
        topic: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.binary = binary
        self.project = str(project) if project is not None else None
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

    async def backlinks(self, target: str) -> dict[str, Any]:
        self.calls.append(("backlinks", target))
        return self._result("backlinks", payload={"target": target, "links": []})

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

    async def research(self, query: str | None = None) -> dict[str, Any]:
        self.calls.append(("research", query))
        return self._result(
            "research", payload={"command": "research", "changed_paths": ["raw/r.md"]}
        )

    async def compile(self, output: str | Path | None = None) -> dict[str, Any]:
        self.calls.append(("compile", str(output) if output is not None else None))
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
def client() -> TestClient:
    app = FastAPI()
    server = SimpleNamespace(services=SimpleNamespace(config=SimpleNamespace()))
    app.include_router(create_wiki_router(server))
    return TestClient(app)


def test_status_search_read_and_gateway_scope(client: TestClient) -> None:
    invalid_scope = client.get("/api/wiki/status", params={"project": "p", "topic": "t"})
    assert invalid_scope.status_code == 400
    assert invalid_scope.json()["detail"] == "Provide project or topic scope, not both"

    search = client.get("/api/wiki/search", params={"query": "hooks", "limit": 3, "project": "p"})
    assert search.status_code == 200
    assert search.json()["payload"] == {"command": "search", "query": "hooks", "limit": 3}
    assert FakeGateway.instances[-1].project == "p"

    no_selector = client.get("/api/wiki/read")
    both_selectors = client.get("/api/wiki/read", params={"path": "a.md", "title": "A"})
    assert no_selector.status_code == 400
    assert both_selectors.status_code == 400

    read = client.get("/api/wiki/read", params={"title": "A"})
    assert read.status_code == 200
    assert read.json()["payload"]["content"] == "# Page\n\nBody"
    assert FakeGateway.instances[-1].calls == [("read", {"path": None, "title": "A"})]


def test_backlinks_health_and_sources_passthrough(client: TestClient) -> None:
    assert (
        client.get("/api/wiki/backlinks", params={"target": "A"}).json()["payload"]["target"] == "A"
    )
    assert client.get("/api/wiki/health").json()["payload"]["status"] == "healthy"
    assert client.get("/api/wiki/sources").json()["payload"]["sources"] == [{"id": "src-1"}]


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
    mixed = client.post(
        "/api/wiki/ingest", json={"path": "a.md", "urls": ["https://example.test/a"]}
    )
    response = client.post(
        "/api/wiki/ingest",
        json={"urls": ["https://example.test/a", "https://example.test/b"]},
    )

    assert mixed.status_code == 400
    assert response.status_code == 200
    assert response.json()["payload"]["accepted"][0]["requested_url"] == "https://example.test/a"
    assert FakeGateway.instances[-1].calls == [
        ("ingest_url", ["https://example.test/a", "https://example.test/b"])
    ]


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
        ("post", "/api/wiki/research", {"json": {"query": "hooks"}}),
        ("post", "/api/wiki/compile", {"json": {"output": "out.md"}}),
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
        "research",
        "compile",
        "remove-source",
    ]


def test_wiki_router_registered_in_app() -> None:
    server = create_http_server(config=DaemonConfig())

    route_paths = {getattr(route, "path", "") for route in server.app.routes}

    assert "/api/wiki/status" in route_paths
    assert "/api/wiki/remove-source" in route_paths


def _handled_results() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for coordinator in RecordingCoordinator.instances:
        results.extend(coordinator.results)
    return results
