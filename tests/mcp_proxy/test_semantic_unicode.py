"""Legacy vector metadata and invalid live schemas stay behind the Unicode boundary."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import ListToolsResult, Tool

from gobby.ai.embeddings import EmbeddingService
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.semantic_search import DEFAULT_EMBEDDING_DIM, SemanticToolSearch
from gobby.mcp_proxy.server import GobbyDaemonTools
from gobby.mcp_proxy.tools.internal import InternalRegistryManager
from gobby.storage.hub.protocol import HubDatabase

HIDDEN = "\u202e\U000e0061"
pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["search", "recommend"])
async def test_semantic_responses_clean_legacy_vector_descriptions(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    surface: str,
) -> None:
    config = MCPServerConfig(
        name="external", project_id="test-project", transport="http", url="https://example.test"
    )
    manager = MCPClientManager(server_configs=[config], project_id="test-project")
    payload = {
        "server_id": config.id,
        "server_name": config.name,
        "tool_name": "ReadExact",
        "description": "Read" + HIDDEN + " records",
    }
    vector_store = AsyncMock()
    vector_store.get_collection_dimension.return_value = DEFAULT_EMBEDDING_DIM
    vector_store.search_with_payload.return_value = [("tool-id", 0.9, payload)]
    monkeypatch.setattr(
        EmbeddingService,
        "generate_embedding",
        AsyncMock(return_value=[0.1] * DEFAULT_EMBEDDING_DIM),
    )
    search = SemanticToolSearch(temp_db, vector_store=vector_store)
    handler = GobbyDaemonTools(
        mcp_manager=manager,
        daemon_port=8787,
        websocket_port=8788,
        start_time=0.0,
        internal_manager=InternalRegistryManager(),
        db=temp_db,
        semantic_search=search,
    )
    caplog.set_level(logging.INFO, logger="gobby.prompts.rendering")

    if surface == "search":
        result = await handler.search_tools(query="read records")
        assert result["success"] is True
        assert result["results"][0]["description"] == "Read records"
        assert result["results"][0]["tool_name"] == "ReadExact"
    else:
        result = await handler.recommend_tools("read records", search_mode="semantic")
        assert result["success"] is True
        assert result["recommendations"][0]["reason"] == "Read records"
        assert result["recommendations"][0]["tool"] == "ReadExact"

    assert result["total_results"] == 1
    assert payload["description"] == "Read" + HIDDEN + " records"
    assert "Invisible Unicode at" in caplog.text
    assert "semantic" in caplog.text
    assert HIDDEN not in caplog.text


@pytest.mark.asyncio
async def test_hidden_schema_key_rejection_does_not_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = MCPServerConfig(
        name="external", project_id="test-project", transport="http", url="https://example.test"
    )
    manager = MCPClientManager(server_configs=[config], project_id="test-project")
    session = SimpleNamespace(
        list_tools=AsyncMock(
            return_value=ListToolsResult(
                tools=[
                    Tool(
                        name="ReadExact",
                        inputSchema={"type": "object", "properties": {"key" + HIDDEN: {}}},
                    )
                ]
            )
        )
    )
    reconnect = AsyncMock(return_value=session)
    monkeypatch.setattr(manager, "get_client_session", AsyncMock(return_value=session))
    monkeypatch.setattr(manager, "ensure_connected", reconnect)

    with pytest.raises(ValueError, match="Untrusted dictionary key"):
        await manager.list_tools(config.id)

    reconnect.assert_not_awaited()
    session.list_tools.assert_awaited_once()
    assert config.id not in manager._tool_schema_cache
    assert config.tools is None or config.tools == []
