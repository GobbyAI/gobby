"""Startup repair must include internal tools, not just cached external servers."""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.persistence import DEFAULT_EMBEDDING_DIM
from gobby.mcp_proxy.semantic_search import SemanticToolSearch
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.runner_init.mcp_stack import (
    SCOPED_PAYLOAD_VERSION_KEY,
    maybe_backfill_scoped_tool_embeddings,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import GLOBAL_PROJECT_ID


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
        filters = kwargs.get("filters") or {}
        rows = [("pipeline", 0.95, payload), ("foreign", 1.0, {**payload, "server_id": "foreign"})]
        return [row for row in rows if all(row[2].get(k) == v for k, v in filters.items())]

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
