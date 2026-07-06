from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.config.app import DaemonConfig
from gobby.gwiki_gateway import (
    GENERATION_GWIKI_TIMEOUT_SECONDS,
    INTERACTIVE_GWIKI_TIMEOUT_SECONDS,
    GwikiCommandError,
)
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.wiki import create_wiki_registry
from gobby.storage.projects import LocalProjectManager

pytestmark = pytest.mark.unit


class FakeGateway:
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

    async def ask(
        self,
        query: str,
        *,
        llm: bool = False,
        ai: str | None = None,
        require_ai: bool = False,
        token_budget: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "ask",
                {
                    "query": query,
                    "llm": llm,
                    "ai": ai,
                    "require_ai": require_ai,
                    "token_budget": token_budget,
                },
            )
        )
        payload = {
            "command": "ask",
            "query": query,
            "status": "retrieved",
            "hits": [],
            "related_pages": [],
            "sources": [],
            "gaps": [],
            "stale_candidates": [],
            "suggested_questions": [],
            "warnings": [],
        }
        if llm:
            payload["ai"] = {"requested": True, "route": "direct", "status": "available"}
            payload["synthesis"] = {"answer": "Hooks run at turn boundaries."}
        return self._result("ask", payload)

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


def _registry() -> InternalToolRegistry:
    return create_wiki_registry(
        gateway_cls=FakeGateway,
        update_coordinator_cls=RecordingCoordinator,
    )


def _schema(name: str) -> dict[str, Any]:
    schema = _registry().get_schema(name)
    assert schema is not None
    return schema["inputSchema"]


@pytest.mark.asyncio
async def test_tool_schemas(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    registry = _registry()
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert {
        "wiki_search",
        "wiki_ask",
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

    ask_schema = _schema("wiki_ask")
    assert {"query", "project", "topic", "llm", "ai", "require_ai", "token_budget"} <= set(
        ask_schema["properties"]
    )
    assert ask_schema["required"] == ["query"]

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

    ask_result = await registry.call(
        "wiki_ask",
        {
            "query": "How do hooks work?",
            "llm": True,
            "ai": "direct",
            "require_ai": True,
            "token_budget": 4096,
            "topic": "docs",
        },
    )
    assert ask_result["payload"]["synthesis"]["answer"] == "Hooks run at turn boundaries."
    assert FakeGateway.instances[-1].calls == [
        (
            "ask",
            {
                "query": "How do hooks work?",
                "llm": True,
                "ai": "direct",
                "require_ai": True,
                "token_budget": 4096,
            },
        ),
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
        _config=DaemonConfig(wiki={"binary": "/bin/gwiki"}),
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
            "ai": "direct",
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
                "ai": "direct",
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
    assert bad_ai["error"] == "ai must be one of auto, daemon, direct, off"
    assert FakeGateway.instances == [] or all(
        call[0] != "compile" for gateway in FakeGateway.instances for call in gateway.calls
    )


@pytest.mark.asyncio
async def test_wiki_compile_uses_generation_gateway_timeout() -> None:
    registry = _registry()

    result = await registry.call("wiki_compile", {"ai": "daemon"})

    assert result["success"] is True
    assert FakeGateway.instances[-1].timeout_seconds == GENERATION_GWIKI_TIMEOUT_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected_timeout"),
    [
        ({"query": "q", "ai": "daemon"}, GENERATION_GWIKI_TIMEOUT_SECONDS),
        ({"query": "q", "ai": "auto"}, GENERATION_GWIKI_TIMEOUT_SECONDS),
        ({"query": "q", "llm": True}, GENERATION_GWIKI_TIMEOUT_SECONDS),
        ({"query": "q", "ai": "off"}, INTERACTIVE_GWIKI_TIMEOUT_SECONDS),
        ({"query": "q"}, INTERACTIVE_GWIKI_TIMEOUT_SECONDS),
    ],
)
async def test_wiki_ask_gateway_timeout_tracks_ai_routing(
    arguments: dict[str, Any], expected_timeout: float
) -> None:
    registry = _registry()

    result = await registry.call("wiki_ask", arguments)

    assert result["success"] is True
    assert FakeGateway.instances[-1].timeout_seconds == expected_timeout


@pytest.mark.asyncio
async def test_wiki_search_keeps_interactive_gateway_timeout() -> None:
    registry = _registry()

    result = await registry.call("wiki_search", {"query": "hooks"})

    assert result["success"] is True
    assert FakeGateway.instances[-1].timeout_seconds == INTERACTIVE_GWIKI_TIMEOUT_SECONDS
