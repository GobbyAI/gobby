"""Keep the native gmcp wire surface equal to the Python stdio wrapper.

``_capture`` drives the real Python wrapper over in-memory MCP streams with a
scripted daemon behind ``httpx.MockTransport``. The fixtures under
``crates/gmcp/contracts`` are that capture, written once with::

    uv run python tests/mcp_proxy/test_gmcp_parity.py
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from pydantic import BaseModel

from gobby.config.bootstrap import BootstrapConfig
from gobby.mcp_proxy.instructions import build_gobby_instructions
from gobby.mcp_proxy.stdio_proxy import (
    _BRIDGE_READY_RETRYABLE_ERROR_CODES,
    BRIDGE_READY_RETRY_DELAYS_SECONDS,
    DaemonProxy,
    DaemonProxyDependencies,
)
from gobby.mcp_proxy.stdio_server import (
    StdioServerDependencies,
    _StdioMCPServer,
    create_stdio_mcp_server,
)
from gobby.mcp_proxy.stdio_tools import register_proxy_tools
from gobby.storage.projects import GLOBAL_PROJECT_ID

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "crates/gmcp/contracts"
TOOLS_LIST_FIXTURE = CONTRACTS_DIR / "tools_list.v1.json"
WIRE_CASES_FIXTURE = CONTRACTS_DIR / "wire_cases.v1.json"
SHAPING_FIXTURE = CONTRACTS_DIR / "shaping_cases.v1.json"

_DAEMON_PORT = 61040
_CWD = "/fixture/cwd"
# stdio_tools reads the working directory only through its own ``os`` name, so
# this stand-in pins that one lookup without patching the process-wide os.getcwd.
_STDIO_TOOLS_OS = SimpleNamespace(getcwd=lambda: _CWD)
_TASKS_LISTING = {
    "success": True,
    "tools": [
        {
            "name": "create_task",
            "description": "Create a task",
            "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
        }
    ],
}

# Every tool except call_tool, which 1.3 forwards. Each call runs once against
# the scripted daemon, and the requests it sends are recorded in order.
_FORWARDED_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_mcp_servers", {}),
    ("list_tools", {"server_name": "gobby-tasks"}),
    ("get_tool_schema", {"server_name": "gobby-tasks", "tool_name": "create_task"}),
    ("recommend_tools", {"task_description": "fix the login bug"}),
    ("search_tools", {"query": "create a task"}),
    ("add_mcp_server", {"name": "demo", "transport": "stdio", "command": "demo-server"}),
    ("remove_mcp_server", {"name": "demo"}),
    ("import_mcp_server", {"query": "github tools"}),
    ("set_variable", {"name": "flag", "value": True, "session_id": "#123"}),
    ("get_variable", {"name": "flag", "session_id": "#123"}),
    ("init_project", {"name": "demo"}),
)

# The reshaped tool results. Each case answers the wrapper's daemon requests in
# order, so the capture records exactly how the wrapper reshapes those answers.
_SHAPING_CASES: tuple[tuple[str, str, dict[str, Any], list[Any]], ...] = (
    (
        "list_mcp_servers_compacts_rows",
        "list_mcp_servers",
        {},
        [
            {
                "success": True,
                "servers": [
                    {
                        "id": "srv-1",
                        "name": "gobby-tasks",
                        "state": "connected",
                        "enabled": True,
                        "transport": "stdio",
                        "scope": "global",
                        "template": None,
                        "description": "tasks",
                    },
                    {
                        "id": "srv-2",
                        "name": "notes",
                        "state": "error",
                        "enabled": False,
                        "transport": "http",
                        "project_id": GLOBAL_PROJECT_ID,
                        "template": "notes-template",
                    },
                    "legacy-name",
                    {"name": ""},
                    42,
                    {"name": "bare"},
                ],
                "total": 4,
                "connected": 1,
                "extra": "kept",
            }
        ],
    ),
    (
        "list_tools_strips_none_for_one_server",
        "list_tools",
        {"server_name": "gobby-tasks"},
        [
            {
                "success": True,
                "server": None,
                "tools": [
                    {
                        "name": "create_task",
                        "description": None,
                        "inputSchema": {
                            "type": "object",
                            "properties": {"title": {"type": "string", "description": None}},
                        },
                    }
                ],
            }
        ],
    ),
    (
        "list_tools_aggregates_servers_without_a_name",
        "list_tools",
        {"server_name": ""},
        [
            {
                "success": True,
                "mcp_servers": {
                    "gobby-tasks": {"state": "connected"},
                    "broken": {"state": "error"},
                    "notes": {"state": "connected"},
                },
            },
            {"success": True, "tools": [{"name": "create_task", "description": None}]},
            {"success": False, "error": "unreachable"},
            {"success": True, "tools": [{"name": "note", "inputSchema": {"type": "object"}}]},
        ],
    ),
    (
        "get_tool_schema_reshapes_the_answer",
        "get_tool_schema",
        {"server_name": "gobby-tasks", "tool_name": "create_task"},
        [
            {
                "name": "create_task",
                "description": None,
                "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}}},
                "server_name": "gobby-tasks",
            }
        ],
    ),
    (
        "get_tool_schema_keeps_only_the_error",
        "get_tool_schema",
        {"server_name": "gobby-tasks", "tool_name": "missing"},
        [{"error": "tool not found", "detail": "dropped"}],
    ),
)


class _ScriptedDaemon:
    """Answers like a healthy daemon and records every request it receives.

    ``script`` supplies the answers in request order and takes precedence over
    the default routes until it is used up.
    """

    def __init__(self, *, reachable: bool = True, script: list[Any] | None = None) -> None:
        self.reachable = reachable
        self.script: list[Any] = list(script or [])
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # The raw target keeps percent-encoding (%23 for "#") as it goes on the wire.
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.raw_path.decode().split("?", 1)[0],
                "query": dict(request.url.params),
                "body": json.loads(request.content) if request.content else None,
            }
        )
        if not self.reachable:
            # A daemon that is not listening refuses the connection.
            raise httpx.ConnectError("connection refused", request=request)
        if self.script:
            return httpx.Response(200, json=self.script.pop(0))
        if request.method == "GET" and request.url.path == "/api/mcp/gobby-tasks/tools":
            return httpx.Response(200, json=_TASKS_LISTING)
        return httpx.Response(200, json={"success": True})


def _proxy(port: int, daemon: _ScriptedDaemon, *, base_url: str | None) -> DaemonProxy:
    proxy_deps = DaemonProxyDependencies(
        check_daemon_http_health=_healthy,
        read_project_id=lambda: None,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(daemon)),
        logger=logging.getLogger("gobby.mcp.stdio"),
    )
    return DaemonProxy(port, deps_factory=lambda: proxy_deps, base_url=base_url)


async def _healthy(
    port: int,
    timeout: float = 2.0,
    *,
    base_url: str | None = None,
    tolerate_busy: bool = False,
) -> bool:
    del port, timeout, base_url, tolerate_busy
    return True


def _server(daemon: _ScriptedDaemon) -> MCPServer[None]:
    def proxy_factory(
        port: int,
        *,
        base_url: str | None = None,
        startup_task: asyncio.Task[None] | None = None,
    ) -> DaemonProxy:
        del startup_task
        return _proxy(port, daemon, base_url=base_url)

    deps = StdioServerDependencies(
        load_bootstrap=lambda: BootstrapConfig(daemon_port=_DAEMON_PORT),
        build_gobby_instructions=build_gobby_instructions,
        mcp_server_factory=_StdioMCPServer,
        proxy_factory=proxy_factory,
        register_proxy_tools=register_proxy_tools,
    )
    return create_stdio_mcp_server(deps=deps)


@contextlib.asynccontextmanager
async def _connected_client(mcp: MCPServer[None]) -> AsyncIterator[ClientSession]:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        server_task = asyncio.create_task(
            mcp._lowlevel_server.run(
                *server_streams,
                mcp._lowlevel_server.create_initialization_options(),
            )
        )
        try:
            async with ClientSession(*client_streams) as session:
                yield session
        finally:
            server_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server_task


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)


def _normalized(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # The readiness report runs on a background task, so it is not part of any call.
    # The working directory belongs to the host, so both sides compare a placeholder.
    normalized = copy.deepcopy(
        [request for request in requests if request["path"] != "/api/mcp/bridge/ready"]
    )
    for request in normalized:
        if isinstance(request["body"], dict) and "cwd" in request["body"]:
            request["body"]["cwd"] = "<cwd>"
    return normalized


async def _capture() -> dict[str, Any]:
    # The host's own identity, auth, daemon URL, and working directory must not
    # leak into the capture.
    with (
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers", return_value={}),
        patch(
            "gobby.mcp_proxy.stdio_server.resolve_daemon_url",
            return_value=f"http://127.0.0.1:{_DAEMON_PORT}",
        ),
        patch("gobby.mcp_proxy.stdio_tools.os", _STDIO_TOOLS_OS),
    ):
        daemon = _ScriptedDaemon()
        async with _connected_client(_server(daemon)) as session:
            initialize = await session.initialize()
            listed = await session.list_tools()
            success = await session.call_tool("list_tools", {"server_name": "gobby-tasks"})
            init_project = await session.call_tool("init_project", {"name": "demo"})
            forwarded: list[dict[str, Any]] = []
            for tool, arguments in _FORWARDED_CALLS:
                start = len(daemon.requests)
                await session.call_tool(tool, arguments)
                forwarded.append(
                    {
                        "tool": tool,
                        "arguments": arguments,
                        "requests": _normalized(daemon.requests[start:]),
                    }
                )

        down = _ScriptedDaemon(reachable=False)
        async with _connected_client(_server(down)) as session:
            await session.initialize()
            unavailable = await session.call_tool("list_mcp_servers", {})

    return {
        "tools_list": _dump(listed),
        "wire_cases": {
            "initialize": _dump(initialize),
            "calls": {
                "list_tools_success": _dump(success),
                "daemon_unavailable": _dump(unavailable),
                "init_project": _dump(init_project),
            },
            "forwarded": forwarded,
            "bridge_ready": {
                "delays_seconds": [0.0, *BRIDGE_READY_RETRY_DELAYS_SECONDS],
                "retry_error_codes": sorted(_BRIDGE_READY_RETRYABLE_ERROR_CODES),
            },
        },
    }


async def _capture_shaping() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    with (
        patch("gobby.mcp_proxy.stdio_proxy.daemon_auth_headers", return_value={}),
        patch(
            "gobby.mcp_proxy.stdio_server.resolve_daemon_url",
            return_value=f"http://127.0.0.1:{_DAEMON_PORT}",
        ),
        patch("gobby.mcp_proxy.stdio_tools.os", _STDIO_TOOLS_OS),
    ):
        for name, tool, arguments, responses in _SHAPING_CASES:
            daemon = _ScriptedDaemon(script=copy.deepcopy(responses))
            async with _connected_client(_server(daemon)) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
            cases.append(
                {
                    "name": name,
                    "tool": tool,
                    "arguments": arguments,
                    "responses": responses,
                    "requests": _normalized(daemon.requests),
                    "envelope": _dump(result),
                }
            )
    return {"cases": cases}


@pytest.mark.asyncio
async def test_fixture_matches_python_wrapper() -> None:
    captured = await _capture()

    assert json.loads(TOOLS_LIST_FIXTURE.read_text()) == captured["tools_list"]
    assert json.loads(WIRE_CASES_FIXTURE.read_text()) == captured["wire_cases"]


@pytest.mark.asyncio
async def test_shaping_fixture_matches_python_wrapper() -> None:
    captured = await _capture_shaping()

    assert json.loads(SHAPING_FIXTURE.read_text()) == captured


def _write_fixtures() -> None:
    captured = asyncio.run(_capture())
    shaping = asyncio.run(_capture_shaping())
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    for path, payload in (
        (TOOLS_LIST_FIXTURE, captured["tools_list"]),
        (WIRE_CASES_FIXTURE, captured["wire_cases"]),
        (SHAPING_FIXTURE, shaping),
    ):
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    _write_fixtures()
