"""Third-party resource reads share the context trust boundary."""

import logging
from unittest.mock import AsyncMock

import pytest
from mcp.types import ReadResourceResult, TextResourceContents

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("model_result", [True, False])
async def test_resource_read_is_cleaned(
    model_result: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = MCPServerConfig(
        name="gobby-evil", project_id="test-project", transport="http", url="https://example.test"
    )
    manager = MCPClientManager(server_configs=[config], project_id="test-project")
    model = ReadResourceResult(
        contents=[
            TextResourceContents(uri="file:///sample", text="private payload\u202e\U000e0061")
        ]
    )
    original = model if model_result else model.model_dump(mode="json")
    monkeypatch.setattr(manager, "read_resource", AsyncMock(return_value=original))
    proxy = ToolProxyService(mcp_manager=manager)
    with caplog.at_level(logging.INFO, logger="gobby.prompts.rendering"):
        result = await proxy.read_resource("gobby-evil", "file:///sample")
    payload = result.model_dump(mode="json") if model_result else result
    assert payload["contents"][0]["text"] == "private payload"
    assert isinstance(model.contents[0], TextResourceContents)
    assert model.contents[0].text == "private payload\u202e\U000e0061"
    assert "tags=1, zero_width=0, bidi=1, controls=0" in caplog.text
    assert "private payload" not in caplog.text
