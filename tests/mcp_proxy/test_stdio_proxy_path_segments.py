"""Keep server, tool and session names one encoded path segment on the Python stdio wrapper.

Each request is recorded from its raw request-target, as the daemon receives it. A
traversal name such as ``../x`` must arrive as ``..%2Fx``, and a bare ``.`` or ``..``
must never leave the wrapper.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from gobby.mcp_proxy.stdio_proxy import DaemonProxy, DaemonProxyDependencies


class _Daemon:
    """Records each request target and answers the status and listing routes."""

    def __init__(self, status: dict[str, Any] | None = None) -> None:
        self.status = status if status is not None else {"success": True, "mcp_servers": {}}
        self.targets: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode().split("?", 1)[0]
        self.targets.append(f"{request.method} {path}")
        if path == "/api/admin/status":
            return httpx.Response(200, json=self.status)
        if path == "/api/mcp/bridge/tool-timeouts":
            return httpx.Response(200, json={"success": True, "tool_timeouts": {}})
        return httpx.Response(200, json={"success": True, "tools": []})


async def _healthy(
    port: int,
    timeout: float = 2.0,
    *,
    base_url: str | None = None,
    tolerate_busy: bool = False,
) -> bool:
    del port, timeout, base_url, tolerate_busy
    return True


def _proxy(daemon: _Daemon) -> DaemonProxy:
    deps = DaemonProxyDependencies(
        check_daemon_http_health=_healthy,
        read_project_id=lambda: None,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(daemon)),
        logger=logging.getLogger("gobby.mcp.stdio"),
    )
    return DaemonProxy(60887, deps_factory=lambda: deps, base_url="http://daemon.test")


async def test_remove_mcp_server_sends_the_name_as_one_segment() -> None:
    daemon = _Daemon()

    await _proxy(daemon).remove_mcp_server("../../projects/p1")

    assert daemon.targets == ["DELETE /api/mcp/servers/..%2F..%2Fprojects%2Fp1"]


async def test_list_tools_sends_the_server_name_as_one_segment() -> None:
    daemon = _Daemon()

    await _proxy(daemon).list_tools(server_name="a/../b")

    assert daemon.targets == ["GET /api/mcp/servers", "GET /api/mcp/a%2F..%2Fb/tools"]


async def test_list_all_skips_empty_and_dot_only_status_names() -> None:
    daemon = _Daemon({"success": True, "mcp_servers": {"": {}, "..": {}, ".": {}, "tasks": {}}})

    await _proxy(daemon).list_tools()

    assert daemon.targets == [
        "GET /api/admin/status",
        "GET /api/mcp/servers",
        "GET /api/mcp/tasks/tools",
    ]


async def test_call_tool_sends_both_names_as_one_segment() -> None:
    daemon = _Daemon()

    await _proxy(daemon).call_tool("../x", "a/b")

    assert daemon.targets == [
        "GET /api/mcp/bridge/tool-timeouts",
        "POST /api/mcp/..%2Fx/tools/a%2Fb",
    ]


async def test_dot_only_names_are_refused_without_a_request() -> None:
    daemon = _Daemon()
    proxy = _proxy(daemon)

    refusals = [
        await proxy.remove_mcp_server(".."),
        await proxy.remove_mcp_server("."),
        await proxy.remove_mcp_server(""),
        await proxy.list_tools(server_name=".."),
        await proxy.list_tools(server_name="."),
        await proxy.call_tool(".", "x"),
        await proxy.call_tool("x", ".."),
    ]

    assert refusals == [
        {"success": False, "error": "name must name one server"},
        {"success": False, "error": "name must name one server"},
        {"success": False, "error": "name must name one server"},
        {"success": False, "error": "server_name must name one server"},
        {"success": False, "error": "server_name must name one server"},
        {"success": False, "error": "server_name/tool_name must name one entry"},
        {"success": False, "error": "server_name/tool_name must name one entry"},
    ]
    assert daemon.targets == []


async def test_session_ids_send_one_encoded_segment() -> None:
    daemon = _Daemon()
    proxy = _proxy(daemon)

    await proxy.set_variable("x", 1, session_id="sess/1")
    await proxy.get_variable("x", session_id="sess/1")

    assert daemon.targets == [
        "POST /api/sessions/sess%2F1/variables/set",
        "POST /api/sessions/sess%2F1/variables/get",
    ]


async def test_empty_or_dot_only_session_ids_are_refused_without_a_request() -> None:
    daemon = _Daemon()
    proxy = _proxy(daemon)

    refusals = [
        await proxy.set_variable("x", 1, session_id=""),
        await proxy.set_variable("x", 1, session_id="."),
        await proxy.set_variable("x", 1, session_id=".."),
        await proxy.get_variable("x", session_id=""),
        await proxy.get_variable("x", session_id="."),
        await proxy.get_variable("x", session_id=".."),
    ]

    refused = {"success": False, "error": "session_id must name one session"}
    assert refusals == [refused] * 6
    assert daemon.targets == []
