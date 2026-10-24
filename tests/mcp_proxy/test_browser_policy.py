"""The shared agent proxy enforces the browser trust boundary."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPError, MCPServerConfig


@pytest.fixture
def browser_manager() -> MCPClientManager:
    return MCPClientManager(
        server_configs=[
            MCPServerConfig(
                id="browser",
                name="playwright",
                project_id="project",
                transport="stdio",
                command="npx",
                runtime_hook="loopback_browser",
            )
        ]
    )


@pytest.mark.asyncio
async def test_browser_inventory_excludes_host_code_and_uploads(
    browser_manager: MCPClientManager,
) -> None:
    tools = [
        {"name": name, "inputSchema": {}}
        for name in ["browser_navigate", "browser_run_code", "browser_file_upload", "upload_file"]
    ]
    with (
        patch.object(browser_manager, "get_client_session", new=AsyncMock()),
        patch.object(
            browser_manager, "_list_tools_from_session", new=AsyncMock(return_value=tools)
        ),
    ):
        inventory = await browser_manager.list_tools("browser")
    assert [tool["name"] for tool in inventory["playwright"]] == ["browser_navigate"]
    assert [tool["name"] for tool in browser_manager._tool_schema_cache["browser"]] == [
        "browser_navigate"
    ]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("browser_run_code", {"code": "async (page) => page.context().browser().newContext()"}),
        ("browser_file_upload", {"paths": ["/etc/hosts"]}),
        ("upload_file", {"filePath": "/etc/hosts", "uid": "1"}),
        ("browser_navigate", {"url": "file:///etc/hosts"}),
        ("navigate_page", {"type": "url", "url": "https://example.com"}),
        ("new_page", {"url": "http://169.254.169.254/"}),
        ("browser_navigate", {"url": "http://localhost.example.com/"}),
    ],
)
@pytest.mark.asyncio
async def test_browser_dispatch_refuses_escape_before_connection(
    browser_manager: MCPClientManager, tool: str, arguments: dict[str, Any]
) -> None:
    session = AsyncMock()
    with patch.object(
        browser_manager, "get_client_session", new=AsyncMock(return_value=session)
    ) as connect:
        with pytest.raises(ValueError, match="Loopback browser"):
            await browser_manager.call_tool("browser", tool, arguments)
    connect.assert_not_awaited()


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:1234/", "http://localhost:5173/", "http://[::1]:1234/"]
)
@pytest.mark.asyncio
async def test_browser_dispatch_allows_loopback(
    browser_manager: MCPClientManager, url: str
) -> None:
    session = AsyncMock()
    session.call_tool.return_value = {"loaded": url}
    with patch.object(browser_manager, "get_client_session", new=AsyncMock(return_value=session)):
        result = await browser_manager.call_tool("browser", "browser_navigate", {"url": url})
    assert result == {"loaded": url}
    session.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_schema_refuses_stale_unsafe_cache(browser_manager: MCPClientManager) -> None:
    browser_manager._tool_schema_cache["browser"] = [
        {"name": "browser_run_code", "inputSchema": {}}
    ]
    with pytest.raises(MCPError, match="not found"):
        await browser_manager.get_tool_info("browser", "browser_run_code")
