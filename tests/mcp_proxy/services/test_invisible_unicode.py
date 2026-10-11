"""External MCP content is cleaned at dispatch, before result handling."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPError, MCPServerConfig
from gobby.mcp_proxy.services.tool_execution import _execute_tool
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["external", "gobby-evil", "gobby-skills"])
@pytest.mark.parametrize("model_result", [True, False])
async def test_external_result_and_own_server_bypass(
    server: str, model_result: bool, caplog: pytest.LogCaptureFixture
) -> None:
    hidden = "private payload\U000e0061\u202e"
    model = CallToolResult(
        content=[TextContent(type="text", text=hidden)],
        structured_content={"nested": [hidden]},
        is_error=True,
    )
    original = model if model_result else model.model_dump(mode="json")
    manager = SimpleNamespace(call_tool=AsyncMock(return_value=original))
    internal = InternalRegistryManager()
    if server == "gobby-skills":
        registry = InternalToolRegistry(server)
        registry.register("fetch", "", {}, lambda: original)
        internal.add_registry(registry)
    service = SimpleNamespace(_internal_manager=internal, _mcp_manager=manager)
    with caplog.at_level(logging.INFO, logger="gobby.prompts.rendering"):
        result = await _execute_tool(
            service=service,
            server_name=server,
            tool_name="fetch",
            arguments={},
            effective_session_id=None,
            project_id=None,
            timeout=None,
        )
    if server == "gobby-skills":
        assert result is original
        assert caplog.records == []
    else:
        payload = result.model_dump(mode="json") if model_result else result
        assert payload["content"][0]["text"] == "private payload"
        assert payload["structured_content"] == {"nested": ["private payload"]}
        assert payload["is_error"] is True
        assert "tags=2" in caplog.text
        assert "bidi=2" in caplog.text
        assert "private payload" not in caplog.text
    assert model.content[0].type == "text"
    assert isinstance(model.content[0], TextContent)
    assert model.content[0].text == hidden


@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["external", "gobby-evil", "gobby-skills"])
async def test_public_proxy_caller_receives_cleaned_external_content(
    server: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = MCPServerConfig(
        name=server, project_id="test-project", transport="http", url="https://example.test"
    )
    manager = MCPClientManager(server_configs=[config], project_id="test-project")
    original = {"text": "private payload\U000e0061\u202e"}
    monkeypatch.setattr(manager, "call_tool", AsyncMock(return_value=original))
    internal = InternalRegistryManager()
    if server == "gobby-skills":
        registry = InternalToolRegistry(server)
        registry.register("fetch", "", {}, lambda: original)
        internal.add_registry(registry)
    proxy = ToolProxyService(
        mcp_manager=manager, internal_manager=internal, validate_arguments=False
    )
    with caplog.at_level(logging.INFO, logger="gobby.prompts.rendering"):
        result = await proxy.call_tool(server, "fetch", {}, enforce_workflow=False, offload=False)
    assert result == (original if server == "gobby-skills" else {"text": "private payload"})
    if server != "gobby-skills":
        assert "tags=1" in caplog.text
        assert "bidi=1" in caplog.text
        assert "private payload" not in caplog.text
    else:
        assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["external", "gobby-evil"])
async def test_downstream_error_text_is_also_untrusted(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, server: str
) -> None:
    config = MCPServerConfig(
        name=server, project_id="test-project", transport="http", url="https://example.test"
    )
    manager = MCPClientManager(server_configs=[config], project_id="test-project")
    monkeypatch.setattr(manager, "call_tool", AsyncMock(side_effect=MCPError("failed\u202e\x00")))
    proxy = ToolProxyService(mcp_manager=manager, validate_arguments=False)
    with caplog.at_level(logging.INFO, logger="gobby.prompts.rendering"):
        result = await proxy.call_tool(server, "fetch", {}, enforce_workflow=False, offload=False)
    assert result["success"] is False
    assert result["error"] == "failed"
    count_logs = [
        record.message for record in caplog.records if record.name == "gobby.prompts.rendering"
    ]
    assert count_logs == [
        "Invisible Unicode at third-party MCP error: tags=0, zero_width=0, bidi=1, controls=1"
    ]
