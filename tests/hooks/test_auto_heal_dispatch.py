"""Tests for auto-heal dispatch: inject_result, block_on_failure, proxy self-routing."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive

pytestmark = pytest.mark.unit


def _make_hook_manager_stub(
    tool_proxy_getter=None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> MagicMock:
    """Create a minimal stub with just the fields _dispatch_mcp_calls needs."""
    from gobby.hooks.hook_manager import HookManager

    stub = MagicMock(spec=HookManager)
    stub.tool_proxy_getter = tool_proxy_getter
    stub._loop = loop
    stub.logger = MagicMock()
    # Bind the real methods to our stub
    stub._dispatch_mcp_calls = HookManager._dispatch_mcp_calls.__get__(stub, HookManager)
    stub._run_coro_blocking = HookManager._run_coro_blocking.__get__(stub, HookManager)
    stub._proxy_self_call = HookManager._proxy_self_call.__get__(stub, HookManager)
    stub._format_discovery_result = HookManager._format_discovery_result
    return stub


def _make_event(
    platform_session_id: str = "plat-sess-1",
    prompt: str = "Fix the auth bug",
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id="ext-sess-1",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": prompt},
        metadata={"_platform_session_id": platform_session_id},
    )


class TestInjectResult:
    """Tests for inject_result flag on mcp_calls."""

    def test_inject_result_captures_result(self) -> None:
        """Calls with inject_result=True return results in dispatch_results."""
        proxy = AsyncMock()
        proxy.list_servers = AsyncMock(
            return_value={
                "success": True,
                "servers": [{"name": "gobby-tasks", "state": "connected"}],
                "total": 1,
                "connected": 1,
            }
        )
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "_proxy",
                "tool": "list_mcp_servers",
                "arguments": {},
                "background": False,
                "inject_result": True,
                "block_on_failure": False,
            }
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        assert len(results) == 1
        assert results[0]["inject_result"] is True
        assert results[0]["success"] is True
        assert results[0]["result"]["servers"] == ["gobby-tasks"]

    def test_no_inject_result_returns_empty(self) -> None:
        """Calls without inject_result/block_on_failure don't appear in results."""
        proxy = AsyncMock()
        proxy.call_tool = AsyncMock(return_value={"success": True})
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-tasks",
                "tool": "list_tasks",
                "arguments": {},
                "background": False,
                "inject_result": False,
                "block_on_failure": False,
            }
        ]

        results = stub._dispatch_mcp_calls(calls, event)
        assert len(results) == 0


class TestBlockOnFailure:
    """Tests for block_on_failure flag on mcp_calls."""

    def test_block_on_failure_stops_chain(self) -> None:
        """When block_on_failure=True and call fails, remaining calls are skipped."""
        proxy = AsyncMock()
        proxy.list_servers = AsyncMock(return_value={"success": True, "servers": []})
        proxy.list_tools = AsyncMock(
            return_value={
                "success": False,
                "error": "Server 'nonexistent' not found",
            }
        )
        proxy.get_tool_schema = AsyncMock(return_value={"success": True})
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "_proxy",
                "tool": "list_mcp_servers",
                "arguments": {},
                "inject_result": True,
                "block_on_failure": False,
            },
            {
                "server": "_proxy",
                "tool": "list_tools",
                "arguments": {"server_name": "nonexistent"},
                "inject_result": True,
                "block_on_failure": True,
            },
            {
                "server": "_proxy",
                "tool": "get_tool_schema",
                "arguments": {"server_name": "nonexistent", "tool_name": "foo"},
                "inject_result": True,
                "block_on_failure": True,
            },
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        # Only 2 results: list_mcp_servers succeeded, list_tools failed and stopped chain
        assert len(results) == 2
        assert results[0]["success"] is True
        assert results[1]["success"] is False
        assert results[1]["tool"] == "list_tools"
        # get_tool_schema was never called
        proxy.get_tool_schema.assert_not_called()

    def test_block_on_failure_success_continues(self) -> None:
        """When block_on_failure=True and call succeeds, chain continues."""
        proxy = AsyncMock()
        proxy.list_servers = AsyncMock(return_value={"success": True, "servers": []})
        proxy.list_tools = AsyncMock(return_value={"success": True, "tools": []})
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "_proxy",
                "tool": "list_mcp_servers",
                "arguments": {},
                "inject_result": True,
                "block_on_failure": True,
            },
            {
                "server": "_proxy",
                "tool": "list_tools",
                "arguments": {"server_name": "gobby-tasks"},
                "inject_result": True,
                "block_on_failure": True,
            },
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        assert len(results) == 2
        assert all(r["success"] for r in results)


class TestBlockOnSuccess:
    """Tests for block_on_success flag on mcp_calls."""

    def test_block_on_success_blocks_when_call_succeeds(self) -> None:
        """When block_on_success=True and call succeeds, result is captured with flag."""
        proxy = AsyncMock()
        proxy.call_tool = AsyncMock(
            return_value={
                "success": True,
                "result": [{"file_path": "src/foo.py", "line_start": 10, "snippet": "class Foo:"}],
            }
        )
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-code",
                "tool": "search_content",
                "arguments": {"query": "TaskValidator"},
                "inject_result": True,
                "block_on_failure": False,
                "block_on_success": True,
            },
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0]["block_on_success"] is True

    def test_block_on_success_allows_when_call_fails(self) -> None:
        """When block_on_success=True and call fails, no block — Grep falls through."""
        proxy = AsyncMock()
        proxy.call_tool = AsyncMock(
            return_value={"success": False, "error": "gobby-code unavailable"}
        )
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-code",
                "tool": "search_content",
                "arguments": {"query": "TaskValidator"},
                "inject_result": True,
                "block_on_failure": False,
                "block_on_success": True,
            },
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        assert len(results) == 1
        assert results[0]["success"] is False
        assert results[0]["block_on_success"] is True

    def test_block_on_failure_takes_precedence_over_block_on_success(self) -> None:
        """When both flags set and call fails, block_on_failure fires (checked first)."""
        proxy = AsyncMock()
        proxy.call_tool = AsyncMock(return_value={"success": False, "error": "server down"})
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-code",
                "tool": "search_content",
                "arguments": {"query": "foo"},
                "inject_result": True,
                "block_on_failure": True,
                "block_on_success": True,
            },
            {
                "server": "gobby-code",
                "tool": "search_symbols",
                "arguments": {"query": "foo"},
                "inject_result": True,
                "block_on_failure": False,
                "block_on_success": True,
            },
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        # block_on_failure stops chain — second call never executes
        assert len(results) == 1
        assert results[0]["success"] is False
        assert results[0]["block_on_failure"] is True


class TestProxySelfRouting:
    """Tests for _proxy/* tool routing to ToolProxyService methods."""

    def test_list_mcp_servers_routes_to_list_servers(self) -> None:
        """_proxy/list_mcp_servers routes to proxy.list_servers()."""
        proxy = AsyncMock()
        proxy.list_servers = AsyncMock(
            return_value={
                "success": True,
                "servers": [{"name": "s1", "state": "connected"}],
            }
        )
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "_proxy",
                "tool": "list_mcp_servers",
                "arguments": {},
                "inject_result": True,
                "block_on_failure": False,
            }
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        proxy.list_servers.assert_called_once()
        assert results[0]["success"] is True
        assert results[0]["result"]["servers"] == ["s1"]

    def test_list_mcp_servers_preserves_issue_details(self) -> None:
        """_proxy/list_mcp_servers keeps details only for servers needing attention."""
        proxy = AsyncMock()
        proxy.list_servers = AsyncMock(
            return_value={
                "success": True,
                "servers": [
                    {"name": "live", "state": "connected", "transport": "internal"},
                    {
                        "name": "stale",
                        "state": "disconnected",
                        "transport": "stdio",
                        "enabled": False,
                    },
                ],
                "total": 2,
                "connected": 1,
            }
        )
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "_proxy",
                "tool": "list_mcp_servers",
                "arguments": {},
                "inject_result": True,
                "block_on_failure": False,
            }
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        assert results[0]["result"] == {
            "success": True,
            "servers": ["live", "stale"],
            "total": 2,
            "connected": 1,
            "issues": [
                {
                    "name": "stale",
                    "state": "disconnected",
                    "transport": "stdio",
                    "enabled": False,
                }
            ],
        }

    def test_list_tools_routes_to_proxy_list_tools(self) -> None:
        """_proxy/list_tools routes to proxy.list_tools(server_name)."""
        proxy = AsyncMock()
        proxy.list_tools = AsyncMock(return_value={"success": True, "tools": []})
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "_proxy",
                "tool": "list_tools",
                "arguments": {"server_name": "gobby-tasks"},
                "inject_result": True,
                "block_on_failure": False,
            }
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        proxy.list_tools.assert_called_once_with("gobby-tasks")
        assert results[0]["success"] is True

    def test_get_tool_schema_routes_to_proxy_get_tool_schema(self) -> None:
        """_proxy/get_tool_schema routes to proxy.get_tool_schema(server, tool)."""
        proxy = AsyncMock()
        proxy.get_tool_schema = AsyncMock(
            return_value={
                "success": True,
                "tool": {"name": "create_task", "inputSchema": {}},
            }
        )
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "_proxy",
                "tool": "get_tool_schema",
                "arguments": {"server_name": "gobby-tasks", "tool_name": "create_task"},
                "inject_result": True,
                "block_on_failure": False,
            }
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        proxy.get_tool_schema.assert_called_once_with("gobby-tasks", "create_task")
        assert results[0]["success"] is True

    def test_unknown_proxy_tool_returns_error(self) -> None:
        """_proxy/unknown_tool returns an error dict."""
        proxy = AsyncMock()
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "_proxy",
                "tool": "nonexistent_tool",
                "arguments": {},
                "inject_result": True,
                "block_on_failure": True,
            }
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        assert len(results) == 1
        assert results[0]["success"] is False
        assert "Unknown _proxy tool" in results[0]["result"]["error"]

    def test_non_proxy_server_uses_call_tool(self) -> None:
        """Non-_proxy servers still route through proxy.call_tool()."""
        proxy = AsyncMock()
        proxy.call_tool = AsyncMock(return_value={"success": True, "result": "ok"})
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-tasks",
                "tool": "list_tasks",
                "arguments": {},
                "inject_result": True,
                "block_on_failure": False,
            }
        ]

        results = stub._dispatch_mcp_calls(calls, event)

        proxy.call_tool.assert_called_once()
        assert results[0]["success"] is True


class TestFormatDiscoveryResult:
    """Tests for _format_discovery_result static method."""

    def test_format_list_mcp_servers(self) -> None:
        from gobby.hooks.hook_manager import HookManager

        dr = {
            "tool": "list_mcp_servers",
            "result": {
                "servers": [
                    {"name": "gobby-tasks", "state": "connected"},
                    {"name": "context7", "state": "connected"},
                ],
            },
        }
        formatted = HookManager._format_discovery_result(dr)
        assert "**Available MCP Servers:**" in formatted
        assert "gobby-tasks" in formatted
        assert "context7" in formatted

    def test_format_list_tools(self) -> None:
        from gobby.hooks.hook_manager import HookManager

        dr = {
            "tool": "list_tools",
            "_args": {"server_name": "gobby-tasks"},
            "result": {
                "tools": [
                    {"name": "create_task", "brief": "Create a new task"},
                    {"name": "list_tasks", "brief": "List tasks"},
                ],
            },
        }
        formatted = HookManager._format_discovery_result(dr)
        assert "**Tools on gobby-tasks:**" in formatted
        assert "create_task" in formatted

    def test_format_get_tool_schema(self) -> None:
        from gobby.hooks.hook_manager import HookManager

        dr = {
            "tool": "get_tool_schema",
            "result": {
                "tool": {
                    "name": "create_task",
                    "description": "Create a new task",
                    "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
                },
            },
        }
        formatted = HookManager._format_discovery_result(dr)
        assert "**Schema for create_task:**" in formatted
        assert '"title"' in formatted

    def test_format_get_skill(self) -> None:
        from gobby.hooks.hook_manager import HookManager

        dr = {
            "tool": "get_skill",
            "result": {
                "skill": {
                    "name": "python",
                    "content": "# Python Best Practices\nUse type hints.",
                },
            },
        }
        formatted = HookManager._format_discovery_result(dr)
        assert formatted == skill_fetch_directive("python")
        assert "# Python Best Practices" not in formatted
        assert "<skill" not in formatted

    def test_format_get_skill_empty_content(self) -> None:
        from gobby.hooks.hook_manager import HookManager

        dr = {
            "tool": "get_skill",
            "result": {"skill": {"name": "empty", "content": ""}},
        }
        formatted = HookManager._format_discovery_result(dr)
        assert formatted == skill_fetch_directive("empty")

    def test_format_list_hubs(self) -> None:
        from gobby.hooks.hook_manager import HookManager

        dr = {
            "tool": "list_hubs",
            "result": {
                "hubs": [
                    {
                        "name": "clawdhub",
                        "type": "clawdhub",
                        "auth_required": False,
                        "auth_configured": True,
                    },
                    {
                        "name": "skillsmp",
                        "type": "skillsmp",
                        "auth_required": True,
                        "auth_key_name": "SKILLSMP_API_KEY",
                        "auth_configured": False,
                    },
                ],
            },
        }

        formatted = HookManager._format_discovery_result(dr)

        assert "<available-skill-hubs>" in formatted
        assert "- clawdhub (clawdhub, auth: not required)" in formatted
        assert "- skillsmp (skillsmp, auth: missing SKILLSMP_API_KEY)" in formatted
        assert 'search_hub(query="...", hub_name="optional")' in formatted

    def test_format_unknown_tool(self) -> None:
        from gobby.hooks.hook_manager import HookManager

        dr = {
            "tool": "some_other_tool",
            "result": {"foo": "bar"},
        }
        formatted = HookManager._format_discovery_result(dr)
        assert "**some_other_tool result:**" in formatted
        assert "bar" in formatted


class TestReturnValueBackwardsCompat:
    """Ensure existing callers work with the new return type."""

    def test_dispatch_returns_list(self) -> None:
        """_dispatch_mcp_calls now returns a list (was None)."""
        proxy = AsyncMock()
        proxy.call_tool = AsyncMock(return_value={"success": True})
        stub = _make_hook_manager_stub(tool_proxy_getter=lambda: proxy, loop=None)
        event = _make_event()

        calls = [
            {
                "server": "gobby-tasks",
                "tool": "sync_export",
                "arguments": {},
                "background": False,
            }
        ]

        result = stub._dispatch_mcp_calls(calls, event)
        assert isinstance(result, list)
        # Non-capturing calls return empty list
        assert len(result) == 0

    def test_no_proxy_getter_returns_empty_list(self) -> None:
        stub = _make_hook_manager_stub(tool_proxy_getter=None)
        event = _make_event()

        result = stub._dispatch_mcp_calls([{"server": "s", "tool": "t", "arguments": {}}], event)
        assert result == []
