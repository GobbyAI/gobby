from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from gobby.config.app import DaemonConfig
from gobby.gwiki_gateway import (
    GENERATION_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS,
    GwikiCommandError,
    GwikiGateway,
)
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.wiki import create_wiki_registry
from gobby.storage.projects import LocalProjectManager
from gobby.wiki.update_coordinator import WikiUpdateCoordinator

pytestmark = pytest.mark.unit


class FakeGateway(GwikiGateway):
    instances: list[FakeGateway] = []
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

    async def index(self) -> dict[str, Any]:
        self.index_calls += 1
        self.calls.append(("index", None))
        return self._result("index", {"indexed": {"documents": 1}})

    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("search", {"query": query, "limit": limit, "token_budget": token_budget})
        )
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
        return self._result(
            "write_page",
            {
                "command": "page-write",
                "path": path,
                "created": True,
                "bytes": len(content.encode()),
                "content_hash": "hash-1",
                "changed_paths": [path],
            },
        )

    async def delete_page(self, *, path: str) -> dict[str, Any]:
        self.calls.append(("delete_page", {"path": path}))
        return self._result(
            "delete_page",
            {"command": "page-delete", "path": path, "changed_paths": [path]},
        )

    async def ingest_url(
        self,
        urls: Sequence[str],
        *,
        max_age_hours: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "ingest_url",
                {"urls": list(urls), "max_age_hours": max_age_hours},
            )
        )
        payload = FakeGateway.next_result or {
            "command": "ingest-url",
            "status": "partial",
            "accepted": [{"requested_url": urls[0], "raw_path": "raw/a.md"}],
            "failed": [{"url": urls[-1], "code": "blocked", "message": "blocked"}],
            "indexed": {"documents": 1, "chunks": 2, "links": 3, "sources": 1},
        }
        return {"ok": True, "command": "ingest_url", "payload": payload, "stderr": "warn"}

    async def compile(
        self,
        topic: str | None = None,
        *,
        kind: str | None = None,
        sources: Sequence[str] | None = None,
        outline: Sequence[str] | None = None,
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
        return self._result("compile", {"command": "compile", "changed_paths": ["wiki/a.md"]})

    async def audit(self) -> dict[str, Any]:
        self.calls.append(("audit", None))
        return self._result("audit", {"command": "audit", "changed_paths": ["audit.md"]})

    async def trust(self) -> dict[str, Any]:
        self.calls.append(("trust", None))
        return self._result(
            "trust",
            {
                "command": "trust",
                "trust_status": {"status": "trusted"},
                "runtime": {"mode": "daemon"},
                "services": {"search": "available"},
                "index_counts": {"documents": 1},
                "degradations": [],
                "freshness": {"status": "fresh"},
                "audit_state": "clean",
                "audit_summary": {"open": 0},
                "link_summary": {"broken": 0},
                "graph_metrics": {"nodes": 1},
                "health_summary": {"status": "healthy"},
            },
        )

    async def health(self) -> dict[str, Any]:
        self.calls.append(("health", None))
        return self._result("health", {"status": "healthy"})

    async def sync_sessions(
        self,
        *,
        archive_dir: str | Path | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        archive_value = str(archive_dir) if archive_dir is not None else None
        self.calls.append(("sync_sessions", {"archive_dir": archive_value, "limit": limit}))
        return self._result(
            "sync_sessions",
            {"command": "sync-sessions", "changed_paths": ["sessions/session-1.md"]},
        )

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


class RecordingCoordinator(WikiUpdateCoordinator):
    instances: list[RecordingCoordinator] = []

    def __init__(self, gateway: GwikiGateway) -> None:
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


def _registry() -> InternalToolRegistry:
    return create_wiki_registry(
        gateway_cls=FakeGateway,
        update_coordinator_cls=RecordingCoordinator,
    )


def _schema(name: str) -> dict[str, Any]:
    schema = _registry().get_schema(name)
    assert schema is not None
    input_schema: dict[str, Any] = schema["inputSchema"]
    return input_schema


@pytest.mark.asyncio
async def test_tool_schemas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = _registry()
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert {
        "wiki_search",
        "wiki_read",
        "wiki_attach",
        "wiki_ingest",
        "wiki_compile",
        "wiki_audit",
        "wiki_trust",
        "wiki_health",
        "wiki_sync_sessions",
        "wiki_list_sources",
        "wiki_remove_source",
    } <= tool_names

    search_schema = _schema("wiki_search")
    assert {"query", "project", "topic", "limit", "token_budget"} <= set(
        search_schema["properties"]
    )
    assert search_schema["required"] == ["query"]
    assert "wiki_ask" not in tool_names

    ingest_schema = _schema("wiki_ingest")
    assert "max_age_hours" in ingest_schema["properties"]
    assert ingest_schema["properties"]["max_age_hours"]["type"] == "integer"
    assert ingest_schema["properties"]["max_age_hours"]["minimum"] == 0
    assert ingest_schema["properties"]["max_age_hours"]["maximum"] == 8760
    assert ingest_schema["required"] == []

    remove_schema = _schema("wiki_remove_source")
    assert remove_schema["required"] == ["id"]
    assert {"dry_run", "yes", "keep_asset"} <= set(remove_schema["properties"])

    compile_schema = _schema("wiki_compile")
    assert {
        "compile_topic",
        "kind",
        "sources",
        "outline",
        "target",
        "write_intent",
        "ai",
        "project",
        "topic",
    } <= set(compile_schema["properties"])
    assert "output" not in compile_schema["properties"]
    assert compile_schema["required"] == []

    trust_schema = _schema("wiki_trust")
    assert {"project", "topic"} <= set(trust_schema["properties"])
    assert trust_schema["required"] == []

    read_result = await registry.call(
        "wiki_read",
        {"title": "Page", "topic": "docs"},
    )
    assert read_result["content"] == "# Page\n\nBody"
    assert read_result["scope"] == {"identity": "topic:docs", "project": None, "topic": "docs"}
    assert FakeGateway.instances[-1].calls == [
        ("read", {"path": None, "title": "Page"}),
    ]

    archive_dir = tmp_path / ".gobby" / "session_transcripts" / "custom"
    sync_result = await registry.call(
        "wiki_sync_sessions",
        {"archive_dir": str(archive_dir), "limit": 3, "topic": "docs"},
    )
    assert sync_result["payload"]["changed_paths"] == ["sessions/session-1.md"]
    assert FakeGateway.instances[-1].calls == [
        (
            "sync_sessions",
            {"archive_dir": str(archive_dir), "limit": 3},
        ),
    ]

    invalid_sync = await registry.call(
        "wiki_sync_sessions",
        {"archive_dir": str(tmp_path / "outside"), "limit": 3, "topic": "docs"},
    )
    assert invalid_sync["ok"] is False
    assert invalid_sync["error"] == "archive_dir must be inside the configured archive root"


@pytest.mark.asyncio
async def test_degradation_passthrough() -> None:
    result = await _registry().call("wiki_search", {"query": "needle", "topic": "docs"})

    assert result["ok"] is True
    assert result["scope"] == {"identity": "topic:docs", "project": None, "topic": "docs"}
    assert result["payload"]["degradation"] == {"status": "partial"}
    assert result["citations"] == [{"path": "raw/result.md", "title": "Result"}]
    assert result["paths"] == {"raw_paths": ["raw/result.md"], "changed_paths": []}


@pytest.mark.asyncio
async def test_wiki_trust_is_read_only_passthrough() -> None:
    result = await _registry().call("wiki_trust", {"topic": "docs"})

    assert result["success"] is True
    assert result["scope"] == {"identity": "topic:docs", "project": None, "topic": "docs"}
    assert result["payload"]["trust_status"] == {"status": "trusted"}
    assert result["payload"]["runtime"] == {"mode": "daemon"}
    assert result["paths"] == {"raw_paths": [], "changed_paths": []}
    assert FakeGateway.instances[-1].calls == [("trust", None)]
    assert RecordingCoordinator.instances == []


@pytest.mark.asyncio
async def test_wiki_trust_maps_gateway_command_errors() -> None:
    class FailingTrustGateway(FakeGateway):
        async def trust(self) -> dict[str, Any]:
            self.calls.append(("trust", None))
            raise GwikiCommandError(
                command="trust",
                argv=("gwiki", "trust"),
                returncode=2,
                stderr="bad scope",
                payload={"error": {"code": "bad_scope"}},
            )

    registry = create_wiki_registry(
        db=None,
        gateway_cls=FailingTrustGateway,
        update_coordinator_cls=RecordingCoordinator,
    )

    result = await registry.call("wiki_trust", {"topic": "docs"})

    assert result["success"] is False
    assert result["command"] == "trust"
    assert result["status"] == "failed"
    assert result["payload"] == {"error": {"code": "bad_scope"}}
    assert result["stderr"] == "bad scope"
    assert result["error"] == {
        "type": "command",
        "returncode": 2,
        "message": "bad scope",
    }
    assert result["scope"] == {"identity": "topic:docs", "project": None, "topic": "docs"}
    assert FakeGateway.instances[-1].calls == [("trust", None)]
    assert RecordingCoordinator.instances == []


@pytest.mark.asyncio
async def test_project_scope_resolves_to_repo_path(temp_db: Any, tmp_path: Path) -> None:
    project = LocalProjectManager(temp_db).create(name="wiki-mcp", repo_path=str(tmp_path))
    registry = create_wiki_registry(
        db=temp_db,
        default_project_id=project.id,
        gateway_cls=FakeGateway,
        update_coordinator_cls=RecordingCoordinator,
    )

    result = await registry.call("wiki_search", {"query": "needle", "project": project.id})

    assert result["scope"] == {
        "identity": f"project:{project.id}",
        "project": project.id,
        "topic": None,
    }
    assert FakeGateway.instances[-1].project == str(tmp_path.resolve())


@pytest.mark.asyncio
async def test_source_lifecycle_passthrough() -> None:
    sources = await _registry().call("wiki_list_sources", {"topic": "docs"})
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


@pytest.mark.asyncio
async def test_wiki_ingest_url_batch_passthrough() -> None:
    result = await _registry().call(
        "wiki_ingest",
        {
            "urls": ["https://example.test/a", "https://example.test/b"],
            "max_age_hours": 0,
        },
    )

    assert FakeGateway.instances[-1].calls == [
        (
            "ingest_url",
            {
                "urls": ["https://example.test/a", "https://example.test/b"],
                "max_age_hours": 0,
            },
        )
    ]
    assert result["payload"]["accepted"] == [
        {"requested_url": "https://example.test/a", "raw_path": "raw/a.md"}
    ]
    assert result["payload"]["failed"] == [
        {"url": "https://example.test/b", "code": "blocked", "message": "blocked"}
    ]
    assert result["payload"]["indexed"] == {"documents": 1, "chunks": 2, "links": 3, "sources": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("max_age_hours", [-1, 8761])
async def test_wiki_ingest_rejects_out_of_range_max_age(max_age_hours: int) -> None:
    result = await _registry().call(
        "wiki_ingest",
        {"urls": ["https://example.test/a"], "max_age_hours": max_age_hours},
    )

    assert result == {
        "success": False,
        "ok": False,
        "error": "max_age_hours must be between 0 and 8760",
    }
    assert FakeGateway.instances == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_input",
    [
        pytest.param({"path": "/tmp/a.md"}, id="path"),
        pytest.param({"paths": ["/tmp/a.md", "/tmp/b.md"]}, id="paths"),
    ],
)
async def test_wiki_ingest_rejects_max_age_for_file_input(
    file_input: dict[str, object],
) -> None:
    result = await _registry().call(
        "wiki_ingest",
        {**file_input, "max_age_hours": 24},
    )

    assert result == {
        "success": False,
        "ok": False,
        "error": "max_age_hours is only valid with urls",
    }
    assert FakeGateway.instances == []


@pytest.mark.asyncio
async def test_wiki_ingest_file_batch_aggregates_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ingest_file(self: FakeGateway, path: str | Path) -> dict[str, Any]:
        self.calls.append(("ingest_file", str(path)))
        if str(path).endswith("bad.md"):
            return {
                "ok": False,
                "command": "ingest_file",
                "payload": {"changed_paths": [str(path)]},
                "stderr": "bad file",
            }
        return {
            "ok": True,
            "command": "ingest_file",
            "payload": {"changed_paths": [str(path)]},
            "stderr": "warning",
        }

    monkeypatch.setattr(FakeGateway, "ingest_file", ingest_file)

    result = await _registry().call("wiki_ingest", {"paths": ["/tmp/good.md", "/tmp/bad.md"]})

    assert result["ok"] is False
    assert result["success"] is False
    assert result["stderr"] == "warning\nbad file"
    assert result["paths"]["changed_paths"] == ["/tmp/good.md", "/tmp/bad.md"]


def test_wiki_registry_registered_and_discoverable() -> None:
    manager = setup_internal_registries(
        config_resolver=lambda: DaemonConfig(wiki={"binary": "/bin/gwiki"}),
        db=None,
    )
    registry = manager.get_registry("gobby-wiki")
    assert registry is not None

    tool_names = {tool["name"] for tool in registry.list_tools()}
    assert {
        "wiki_search",
        "wiki_read",
        "wiki_trust",
        "wiki_list_sources",
        "wiki_remove_source",
    } <= tool_names


@pytest.mark.asyncio
async def test_write_tools_delegate_to_coordinator() -> None:
    registry = _registry()

    attach = await registry.call("wiki_attach", {"path": "/tmp/a.md"})
    ingest = await registry.call("wiki_ingest", {"path": "/tmp/b.md"})
    url_ingest = await registry.call("wiki_ingest", {"urls": ["https://example.test/a"]})
    compile_result = await registry.call("wiki_compile", {"target": "/tmp/wiki.md"})
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


@pytest.mark.asyncio
async def test_wiki_write_and_delete_page_tools() -> None:
    registry = _registry()
    tool_names = {tool["name"] for tool in registry.list_tools()}
    assert {"wiki_write_page", "wiki_delete_page"} <= tool_names
    assert "wiki_graph" not in tool_names

    written = await registry.call(
        "wiki_write_page",
        {
            "path": "knowledge/notes/demo.md",
            "content": "# Demo\n",
            "mode": "create",
            "expected_hash": "deadbeef",
        },
    )

    assert written["success"] is True
    assert written["index_handoff"] == {"status": "handled"}
    assert written["paths"]["changed_paths"] == ["knowledge/notes/demo.md"]
    assert FakeGateway.instances[-1].calls == [
        (
            "write_page",
            {
                "path": "knowledge/notes/demo.md",
                "content": "# Demo\n",
                "mode": "create",
                "expected_hash": "deadbeef",
            },
        )
    ]
    assert FakeGateway.instances[-1].timeout_seconds == INTERACTIVE_GWIKI_TIMEOUT_SECONDS

    deleted = await registry.call(
        "wiki_delete_page",
        {"path": "knowledge/notes/demo.md"},
    )

    assert deleted["success"] is True
    assert deleted["index_handoff"] == {"status": "handled"}
    assert FakeGateway.instances[-1].calls == [("delete_page", {"path": "knowledge/notes/demo.md"})]

    bad_mode = await registry.call(
        "wiki_write_page",
        {"path": "knowledge/notes/demo.md", "content": "x", "mode": "replace"},
    )
    assert bad_mode["success"] is False
    assert "mode must be one of create, upsert" in bad_mode["error"]


@pytest.mark.asyncio
async def test_wiki_write_page_surfaces_confinement_errors() -> None:
    class ConfinementRejectingGateway(FakeGateway):
        async def write_page(
            self,
            *,
            path: str,
            content: str,
            mode: str = "upsert",
            expected_hash: str | None = None,
        ) -> dict[str, Any]:
            raise GwikiCommandError(
                command="write_page",
                argv=("gwiki", "page", "write", "--path", path),
                returncode=2,
                stderr="Page paths must live under knowledge/ (invalid_input)",
                payload={"code": "invalid_input", "message": "page path escapes the wiki vault"},
            )

    registry = create_wiki_registry(
        db=None,
        gateway_cls=ConfinementRejectingGateway,
        update_coordinator_cls=RecordingCoordinator,
    )

    result = await registry.call(
        "wiki_write_page",
        {"path": "../escape.md", "content": "x", "topic": "docs"},
    )

    assert result["success"] is False
    assert result["payload"]["code"] == "invalid_input"
    assert "knowledge/" in result["stderr"]
    assert RecordingCoordinator.instances == []


@pytest.mark.asyncio
async def test_wiki_compile_passes_full_param_surface() -> None:
    registry = _registry()

    result = await registry.call(
        "wiki_compile",
        {
            "compile_topic": "Hooks Overview",
            "kind": "Topic",
            "sources": ["src-1", "src-2"],
            "outline": ["Intro"],
            "target": "knowledge/topics/hooks.md",
            "write_intent": True,
            "ai": "off",
            "topic": "docs",
        },
    )

    assert result["success"] is True
    assert result["scope"] == {"identity": "topic:docs", "project": None, "topic": "docs"}
    assert FakeGateway.instances[-1].calls == [
        (
            "compile",
            {
                "topic": "Hooks Overview",
                "kind": "topic",
                "sources": ["src-1", "src-2"],
                "outline": ["Intro"],
                "target": "knowledge/topics/hooks.md",
                "write_intent": True,
                "ai": "off",
            },
        )
    ]


@pytest.mark.asyncio
async def test_wiki_compile_rejects_unknown_kind_and_ai() -> None:
    registry = _registry()

    bad_kind = await registry.call("wiki_compile", {"kind": "article"})
    assert bad_kind["ok"] is False
    assert bad_kind["error"] == "kind must be one of concept, source, topic"

    bad_ai = await registry.call("wiki_compile", {"ai": "cloud"})
    assert bad_ai["ok"] is False
    assert bad_ai["error"] == "ai must be one of auto, off"
    assert FakeGateway.instances == [] or all(
        call[0] != "compile" for gateway in FakeGateway.instances for call in gateway.calls
    )


@pytest.mark.asyncio
async def test_wiki_compile_uses_generation_gateway_timeout() -> None:
    registry = _registry()

    result = await registry.call("wiki_compile", {"ai": "auto"})

    assert result["success"] is True
    assert FakeGateway.instances[-1].timeout_seconds == GENERATION_GWIKI_TIMEOUT_SECONDS

    result = await registry.call("wiki_compile", {})

    assert result["success"] is True
    assert FakeGateway.instances[-1].timeout_seconds == GENERATION_GWIKI_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_wiki_search_keeps_interactive_gateway_timeout() -> None:
    registry = _registry()

    result = await registry.call("wiki_search", {"query": "hooks"})

    assert result["success"] is True
    assert FakeGateway.instances[-1].timeout_seconds == INTERACTIVE_GWIKI_TIMEOUT_SECONDS


async def test_wiki_health_uses_health_gateway_timeout() -> None:
    registry = _registry()

    result = await registry.call("wiki_health", {})

    assert result["success"] is True
    assert FakeGateway.instances[-1].timeout_seconds == INTERACTIVE_HEALTH_GWIKI_TIMEOUT_SECONDS
