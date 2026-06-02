from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.tools.wiki import create_wiki_registry


class FakeGateway:
    instances: list[FakeGateway] = []
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

    async def index(self) -> dict[str, Any]:
        self.index_calls += 1
        self.calls.append(("index", None))
        return self._result("index", {"indexed": {"documents": 1}})

    async def search(self, query: str, *, limit: int | None = None) -> dict[str, Any]:
        self.calls.append(("search", {"query": query, "limit": limit}))
        payload = {
            "command": "search",
            "query": query,
            "results": [{"title": "Result", "raw_path": "raw/result.md"}],
            "citations": [{"path": "raw/result.md", "title": "Result"}],
            "degradation": {"status": "partial"},
        }
        return self._result("search", payload)

    async def read(
        self,
        *,
        path: str | Path | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("read", {"path": str(path) if path is not None else None, "title": title})
        )
        payload = {
            "command": "read",
            "path": str(path) if path is not None else None,
            "title": title,
            "content": "# Page\n\nBody",
            "status": "found",
        }
        return self._result("read", payload)

    async def ingest_file(self, path: str | Path) -> dict[str, Any]:
        self.calls.append(("ingest_file", str(path)))
        return self._result(
            "ingest_file",
            {"command": "ingest-file", "changed_paths": [str(path)]},
        )

    async def ingest_url(self, urls: list[str]) -> dict[str, Any]:
        self.calls.append(("ingest_url", list(urls)))
        payload = FakeGateway.next_result or {
            "command": "ingest-url",
            "status": "partial",
            "accepted": [{"requested_url": urls[0], "raw_path": "raw/a.md"}],
            "failed": [{"url": urls[-1], "code": "blocked", "message": "blocked"}],
            "indexed": {"documents": 1, "chunks": 2, "links": 3, "sources": 1},
        }
        return {"ok": True, "command": "ingest_url", "payload": payload, "stderr": "warn"}

    async def compile(self, output: str | Path | None = None) -> dict[str, Any]:
        self.calls.append(("compile", str(output) if output is not None else None))
        return self._result("compile", {"command": "compile", "changed_paths": ["wiki/a.md"]})

    async def audit(self) -> dict[str, Any]:
        self.calls.append(("audit", None))
        return self._result("audit", {"command": "audit", "changed_paths": ["audit.md"]})

    async def health(self) -> dict[str, Any]:
        self.calls.append(("health", None))
        return self._result("health", {"status": "healthy"})

    async def sources(self) -> dict[str, Any]:
        self.calls.append(("sources", None))
        return self._result("sources", {"sources": [{"id": "src-1"}]})

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
        return self._result(
            "remove_source",
            {
                "command": "remove-source",
                "source": {"id": source_id},
                "dry_run": dry_run,
                "preview": [{"path": "raw/a.md"}] if dry_run else None,
                "index_status": {"index_required": yes},
            },
        )

    def _result(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "command": command, "payload": payload, "stderr": ""}


class RecordingCoordinator:
    instances: list[RecordingCoordinator] = []

    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self.handled: list[dict[str, Any]] = []
        RecordingCoordinator.instances.append(self)

    async def handle_write_result(self, result: dict[str, Any]) -> dict[str, Any]:
        self.handled.append(result)
        return {**result, "index_handoff": {"status": "handled"}}


@pytest.fixture(autouse=True)
def reset_fakes() -> None:
    FakeGateway.instances = []
    FakeGateway.next_result = None
    RecordingCoordinator.instances = []


def _registry():
    return create_wiki_registry(
        config=DaemonConfig(wiki={"binary": "/bin/gwiki", "timeout_seconds": 4}),
        gateway_cls=FakeGateway,
        update_coordinator_cls=RecordingCoordinator,
    )


def _schema(name: str) -> dict[str, Any]:
    schema = _registry().get_schema(name)
    assert schema is not None
    return schema["inputSchema"]


async def test_tool_schemas() -> None:
    registry = _registry()
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert {
        "wiki_search",
        "wiki_read",
        "wiki_attach",
        "wiki_ingest",
        "wiki_compile",
        "wiki_audit",
        "wiki_health",
        "wiki_list_sources",
        "wiki_remove_source",
    } <= tool_names

    search_schema = _schema("wiki_search")
    assert {"query", "project", "topic", "limit"} <= set(search_schema["properties"])
    assert search_schema["required"] == ["query"]

    remove_schema = _schema("wiki_remove_source")
    assert remove_schema["required"] == ["id"]
    assert {"dry_run", "yes", "keep_asset"} <= set(remove_schema["properties"])

    read_result = await registry.call(
        "wiki_read",
        {"title": "Page", "project": "/repo"},
    )
    assert read_result["content"] == "# Page\n\nBody"
    assert read_result["scope"] == {"project": "/repo", "topic": None}
    assert FakeGateway.instances[-1].calls == [
        ("read", {"path": None, "title": "Page"}),
    ]


async def test_degradation_passthrough() -> None:
    result = await _registry().call("wiki_search", {"query": "needle", "topic": "docs"})

    assert result["ok"] is True
    assert result["scope"] == {"project": None, "topic": "docs"}
    assert result["payload"]["degradation"] == {"status": "partial"}
    assert result["citations"] == [{"path": "raw/result.md", "title": "Result"}]
    assert result["paths"] == {"raw_paths": ["raw/result.md"], "changed_paths": []}


async def test_source_lifecycle_passthrough() -> None:
    sources = await _registry().call("wiki_list_sources", {"project": "/repo"})
    assert sources["payload"] == {"sources": [{"id": "src-1"}]}

    preview = await _registry().call(
        "wiki_remove_source",
        {"id": "src-1", "dry_run": True, "keep_asset": True},
    )
    assert FakeGateway.instances[-1].calls == [
        (
            "remove_source",
            {"id": "src-1", "dry_run": True, "yes": False, "keep_asset": True},
        )
    ]
    assert preview["payload"]["preview"] == [{"path": "raw/a.md"}]
    assert preview["index_handoff"] == {"status": "handled"}

    removed = await _registry().call("wiki_remove_source", {"id": "src-1", "yes": True})
    assert FakeGateway.instances[-1].calls == [
        (
            "remove_source",
            {"id": "src-1", "dry_run": False, "yes": True, "keep_asset": False},
        )
    ]
    assert removed["payload"]["index_status"] == {"index_required": True}

    conflict = await _registry().call(
        "wiki_remove_source",
        {"id": "src-1", "dry_run": True, "yes": True},
    )
    assert conflict["success"] is False
    assert "dry_run and yes cannot both be true" in conflict["error"]


async def test_wiki_ingest_url_batch_passthrough() -> None:
    result = await _registry().call(
        "wiki_ingest",
        {"urls": ["https://example.test/a", "https://example.test/b"]},
    )

    assert FakeGateway.instances[-1].calls == [
        ("ingest_url", ["https://example.test/a", "https://example.test/b"])
    ]
    assert result["payload"]["accepted"] == [
        {"requested_url": "https://example.test/a", "raw_path": "raw/a.md"}
    ]
    assert result["payload"]["failed"] == [
        {"url": "https://example.test/b", "code": "blocked", "message": "blocked"}
    ]
    assert result["payload"]["indexed"] == {"documents": 1, "chunks": 2, "links": 3, "sources": 1}


def test_wiki_registry_registered_and_discoverable() -> None:
    manager = setup_internal_registries(
        _config=DaemonConfig(wiki={"binary": "/bin/gwiki"}),
        db=None,
    )
    registry = manager.get_registry("gobby-wiki")
    assert registry is not None

    tool_names = {tool["name"] for tool in registry.list_tools()}
    assert {"wiki_search", "wiki_read", "wiki_list_sources", "wiki_remove_source"} <= tool_names


async def test_write_tools_delegate_to_coordinator() -> None:
    registry = _registry()

    attach = await registry.call("wiki_attach", {"path": "/tmp/a.md"})
    ingest = await registry.call("wiki_ingest", {"path": "/tmp/b.md"})
    url_ingest = await registry.call("wiki_ingest", {"urls": ["https://example.test/a"]})
    compile_result = await registry.call("wiki_compile", {"output": "/tmp/wiki.md"})
    audit = await registry.call("wiki_audit", {})
    removed = await registry.call("wiki_remove_source", {"id": "src-1", "yes": True})

    assert [
        result["index_handoff"]
        for result in (attach, ingest, url_ingest, compile_result, audit, removed)
    ] == [
        {"status": "handled"},
        {"status": "handled"},
        {"status": "handled"},
        {"status": "handled"},
        {"status": "handled"},
        {"status": "handled"},
    ]
    assert [
        coordinator.handled[0]["command"] for coordinator in RecordingCoordinator.instances
    ] == [
        "ingest_file",
        "ingest_file",
        "ingest_url",
        "compile",
        "audit",
        "remove_source",
    ]
    assert all(gateway.index_calls == 0 for gateway in FakeGateway.instances)
