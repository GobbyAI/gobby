"""Startup repair must include internal tools, not just cached external servers."""

import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from qdrant_client.local.payload_filters import check_filter
from qdrant_client.models import Filter

from gobby.config.persistence import DEFAULT_EMBEDDING_DIM
from gobby.mcp_proxy.semantic_search import SemanticToolSearch
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.services.fallback import ToolFallbackResolver
from gobby.mcp_proxy.services.recommendation import RecommendationService
from gobby.mcp_proxy.stdio_proxy import DaemonProxy, DaemonProxyDependencies
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.memory.vectorstore import VectorStore
from gobby.runner_init.mcp_stack import (
    SCOPED_PAYLOAD_VERSION_KEY,
    maybe_backfill_scoped_tool_embeddings,
)
from gobby.servers.routes.mcp.endpoints.discovery import search_mcp_tools
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import GLOBAL_PROJECT_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entrypoint",
    ["semantic", "http", "stdio", "protocol", "recommend_semantic", "recommend_hybrid", "fallback"],
)
async def test_visible_tools_are_filtered_before_vector_top_k(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    collection = "tool-visibility-proof"
    store = VectorStore(path=str(tmp_path / "vectors"), collection_name=collection, embedding_dim=2)
    search = SemanticToolSearch(
        temp_db, vector_store=store, collection_name=collection, embedding_dim=2
    )
    monkeypatch.setattr(search, "embed_text", AsyncMock(return_value=[1.0, 0.0]))
    internal = InternalRegistryManager()
    internal.add_registry(InternalToolRegistry("gobby-workflows"))
    manager = SimpleNamespace(
        project_id="project",
        server_configs=[
            SimpleNamespace(id="local", name="custom", project_id="project"),
            SimpleNamespace(id="shadowed", name="custom", project_id=GLOBAL_PROJECT_ID),
        ],
    )
    tools = GobbyDaemonTools(
        mcp_manager=cast(Any, manager),
        daemon_port=8787,
        websocket_port=8788,
        start_time=0,
        internal_manager=internal,
        db=None,
        semantic_search=search,
    )
    try:
        await store.ensure_collection(collection, 2)
        for server, project, vector, count in (
            ("foreign", "foreign-project", [1.0, 0.0], 20),
            ("shadowed", GLOBAL_PROJECT_ID, [0.999, 0.001], 10),
            ("gobby-workflows", GLOBAL_PROJECT_ID, [0.9, 0.1], 1),
            ("local", "project", [0.8, 0.2], 1),
        ):
            for _ in range(count):
                await store.upsert(
                    str(uuid4()),
                    vector,
                    {
                        "server_id": server,
                        "server_name": server,
                        "project_id": project,
                        "tool_name": "run_pipeline",
                        "description": "Run a pipeline runbook",
                    },
                )
        if entrypoint in {"http", "stdio"}:
            request = MagicMock()
            request.json = AsyncMock(return_value={"query": "run pipeline runbook", "top_k": 2})
            http_server = SimpleNamespace(
                resolve_project_id=lambda *_: "project",
                _tools_handler=SimpleNamespace(_semantic_search=search),
                mcp_manager=manager,
                _mcp_db_manager=manager,
                _internal_manager=internal,
            )
            if entrypoint == "stdio":
                app = FastAPI()

                @app.post("/api/mcp/tools/search")
                async def endpoint(request: Request) -> dict[str, Any]:
                    return await search_mcp_tools(request, cast(Any, http_server))

                monkeypatch.setattr("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers", lambda: {})
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                    deps = DaemonProxyDependencies(
                        check_daemon_http_health=AsyncMock(return_value=True),
                        read_project_id=lambda: "project",
                        http_client_factory=lambda: client,
                        logger=logging.getLogger(__name__),
                    )
                    proxy = DaemonProxy(8787, deps_factory=lambda: deps)
                    response = await proxy.search_tools("run pipeline runbook", top_k=2)
            else:
                response = await search_mcp_tools(request, cast(Any, http_server))
            assert response["success"], response
            names = [row["server_name"] for row in response["results"]]
        elif entrypoint == "protocol":
            response = await tools.search_tools("run pipeline runbook", top_k=2)
            assert response["success"], response
            names = [row["server_name"] for row in response["results"]]
        elif entrypoint.startswith("recommend_"):
            recommendation = RecommendationService(None, manager, db=None, semantic_search=search)
            response = await recommendation.recommend_tools(
                "run pipeline runbook",
                top_k=2,
                search_mode=cast(Any, entrypoint.removeprefix("recommend_")),
                project_id="project",
            )
            assert response["success"], response
            names = [row["server"] for row in response["recommendations"]]
        elif entrypoint == "fallback":
            found_alternatives = await ToolFallbackResolver(search).find_alternatives(
                "unavailable",
                project_id="project",
                top_k=2,
            )
            names = [result.server_name for result in found_alternatives]
        else:
            found = await search.search_tools(
                "run pipeline runbook",
                "project",
                top_k=2,
            )
            names = [result.server_name for result in found]
        assert names == ["gobby-workflows", "local"]
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [True, False])
async def test_runtime_backfill_embeds_external_tools_with_valid_point_ids(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached: bool
) -> None:
    store = VectorStore(path=str(tmp_path / "vectors"), embedding_dim=2)
    search = SemanticToolSearch(temp_db, vector_store=store, embedding_dim=2)
    monkeypatch.setattr(search, "embed_text", AsyncMock(return_value=[1.0, 0.0]))
    tool_id = str(uuid4())
    full_tool = SimpleNamespace(
        id=tool_id, name="navigate", description="Navigate to a URL", input_schema={}
    )
    config = SimpleNamespace(
        id="external",
        name="browser",
        project_id="project",
        tools=[{"name": "navigate", "brief": "Navigate to a URL"}],
    )
    manager = SimpleNamespace(
        server_configs=[config],
        mcp_db_manager=SimpleNamespace(get_cached_tools=lambda _: [full_tool]) if cached else None,
    )
    internal = InternalRegistryManager()
    registry = InternalToolRegistry("gobby-workflows")
    registry.register(
        "run_pipeline", description="Run a pipeline runbook", input_schema={}, func=lambda: None
    )
    internal.add_registry(registry)
    try:
        if cached:
            await search.embed_tool(
                tool_id=str(uuid4()),
                name="navigate",
                description="Navigate to a URL",
                input_schema={},
                server_name="browser",
                project_id="project",
                server_id="external",
            )
        stats = await search.embed_all_tools("project", manager, internal_manager=internal)
        assert stats["failed"] == 0, stats
        assert stats["embedded"] == 2, stats
        found = await search.search_tools(
            "run pipeline runbook", "project", mcp_manager=manager, internal_manager=internal
        )
        assert {result.server_name for result in found} == {"browser", "gobby-workflows"}
        assert len(found) == 2
        external = next(result for result in found if result.server_name == "browser")
        assert external.description == "Navigate to a URL"
        if cached:
            manager.mcp_db_manager = None
            await search.embed_all_tools("project", manager, internal_manager=internal)
            manager.mcp_db_manager = SimpleNamespace(get_cached_tools=lambda _: [full_tool])
            await search.embed_all_tools("project", manager, internal_manager=internal)
            repeated = await search.search_tools(
                "navigate", "project", mcp_manager=manager, internal_manager=internal
            )
            assert len([result for result in repeated if result.server_name == "browser"]) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_id", [True, False])
async def test_backfill_refuses_unscoped_servers_and_offloads_cache_reads(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_id: bool
) -> None:
    store = VectorStore(path=str(tmp_path / "vectors"), embedding_dim=2)
    search = SemanticToolSearch(temp_db, vector_store=store, embedding_dim=2)
    monkeypatch.setattr(search, "embed_text", AsyncMock(return_value=[1.0, 0.0]))
    threads: list[int] = []
    tool = SimpleNamespace(
        id=str(uuid4()), name="navigate", description="Navigate", input_schema={}
    )

    def cached_tools(_: str) -> list[Any]:
        threads.append(threading.get_ident())
        return [tool]

    configs = [SimpleNamespace(id="server", name="browser", project_id="project")]
    if missing_id:
        configs.append(
            SimpleNamespace(
                id=None, name="unscoped", project_id="project", tools=[{"name": "navigate"}]
            )
        )
    manager = SimpleNamespace(
        get_cached_tools=cached_tools,
        server_configs=configs,
    )
    try:
        stats = await search.embed_all_tools("project", manager)
        assert stats["embedded"] == 1, stats
        assert stats["failed"] == int(missing_id), stats
        assert threads and all(thread != threading.get_ident() for thread in threads)
        found = await search.search_tools("navigate", "project")
        assert [result.server_name for result in found] == ["browser"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_backfill_completion_persists_through_real_config_store(temp_db: HubDatabase) -> None:
    config = ConfigStore(temp_db)
    search = SimpleNamespace(embed_all_tools=AsyncMock(return_value={"embedded": 1, "failed": 0}))
    manager = SimpleNamespace(server_configs=[])
    internal = InternalRegistryManager()

    first = await maybe_backfill_scoped_tool_embeddings(
        search, manager, config_store=config, internal_manager=internal
    )
    second = await maybe_backfill_scoped_tool_embeddings(
        search, manager, config_store=ConfigStore(temp_db), internal_manager=internal
    )

    assert first["rewritten"] is True
    assert second["rewritten"] is False
    search.embed_all_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_repairs_previously_marked_partial_inventory() -> None:
    registry = InternalToolRegistry("gobby-workflows")
    registry.register(
        name="run_pipeline",
        description="Run a pipeline runbook.",
        input_schema={"type": "object"},
        func=lambda: None,
    )
    internal = InternalRegistryManager()
    internal.add_registry(registry)
    config: dict[str, Any] = {SCOPED_PAYLOAD_VERSION_KEY: 1}
    search = SimpleNamespace(embed_all_tools=AsyncMock(return_value={"embedded": 1, "failed": 0}))
    external = SimpleNamespace(server_configs=[])

    result = await maybe_backfill_scoped_tool_embeddings(
        search,
        external,
        config_store=SimpleNamespace(get=config.get, set=config.__setitem__),
        internal_manager=internal,
    )

    assert result["rewritten"] is True
    assert search.embed_all_tools.await_args.kwargs["internal_manager"] is internal
    assert config[SCOPED_PAYLOAD_VERSION_KEY] > 1


@pytest.mark.asyncio
async def test_partial_backfill_does_not_commit_completion_marker() -> None:
    config: dict[str, Any] = {}
    search = SimpleNamespace(embed_all_tools=AsyncMock(return_value={"embedded": 4, "failed": 1}))

    await maybe_backfill_scoped_tool_embeddings(
        search,
        SimpleNamespace(server_configs=[]),
        config_store=SimpleNamespace(get=config.get, set=config.__setitem__),
    )

    assert SCOPED_PAYLOAD_VERSION_KEY not in config


@pytest.mark.asyncio
@pytest.mark.parametrize("server", [None, "gobby-workflows", "gobby-annotate"])
async def test_pipeline_discovery_uses_complete_visible_inventory(
    temp_db: HubDatabase, server: str | None
) -> None:
    registry = InternalToolRegistry("gobby-workflows")
    registry.register(
        name="run_pipeline",
        description="Run a pipeline runbook.",
        input_schema={"type": "object"},
        func=lambda: None,
    )
    internal = InternalRegistryManager()
    internal.add_registry(registry)
    payload = {
        "project_id": GLOBAL_PROJECT_ID,
        "server_id": registry.name,
        "server_name": registry.name,
        "tool_name": "run_pipeline",
        "description": "Run a pipeline runbook.",
    }
    vector = AsyncMock()
    vector.get_collection_dimension.return_value = DEFAULT_EMBEDDING_DIM

    async def query_points(**kwargs: Any) -> list[tuple[str, float, dict[str, Any]]]:
        filters = kwargs["filters"]
        assert isinstance(filters, Filter)
        rows = [("pipeline", 0.95, payload), ("foreign", 1.0, {**payload, "server_id": "foreign"})]
        return [row for row in rows if check_filter(filters, row[2], row[0], {})]

    vector.search_with_payload.side_effect = query_points
    search = SemanticToolSearch(temp_db, vector_store=vector)
    cast(Any, search).embed_text = AsyncMock(return_value=[0.1] * DEFAULT_EMBEDDING_DIM)
    tools = GobbyDaemonTools(
        mcp_manager=MagicMock(project_id="caller-project", server_configs=[]),
        daemon_port=8787,
        websocket_port=8788,
        start_time=0,
        internal_manager=internal,
        db=None,
        semantic_search=search,
    )

    result = await tools.search_tools("run pipeline runbook", top_k=5, server_name=server)

    assert [row["tool_name"] for row in result["results"]] == (
        [] if server == "gobby-annotate" else ["run_pipeline"]
    )
