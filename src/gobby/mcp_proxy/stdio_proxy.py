"""HTTP daemon proxy used by the stdio MCP wrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from gobby.cli.runtime import CliRuntime
from gobby.mcp_proxy.daemon_control import check_daemon_http_health as _check_daemon_http_health
from gobby.mcp_proxy.models import ToolProxyErrorCode
from gobby.mcp_proxy.server_list import compact_mcp_server_list
from gobby.mcp_proxy.stdio_results import (
    DAEMON_PROXY_PREFLIGHT_CACHE_SECONDS,
    DAEMON_PROXY_PREFLIGHT_TIMEOUT_SECONDS,
    REMOVED_WORKFLOW_WAIT_TOOL,
    _daemon_unavailable_result,
    _removed_wait_for_completion_result,
    _request_timeout_result,
    _strip_none,
)
from gobby.mcp_proxy.terminal_context import (
    current_terminal_context,
    serialize_terminal_context,
)
from gobby.mcp_proxy.wait_tools import (
    EXTENDED_TIMEOUT_TOOL_NAMES,
    MCP_WRAPPER_PROTOCOL_VERSION,
    MCP_WRAPPER_PROTOCOL_VERSION_HEADER,
    WAIT_TOOL_HTTP_TIMEOUT_BUFFER_SECONDS,
    WAIT_TOOL_NAMES,
)
from gobby.utils.local_token import daemon_auth_headers
from gobby.utils.session_context import AGENT_RUN_ID_HEADER, TERMINAL_CONTEXT_HEADER


class CheckDaemonHealth(Protocol):
    def __call__(
        self,
        port: int,
        timeout: float = 5.0,
        *,
        base_url: str | None = None,
    ) -> Awaitable[bool]: ...


@dataclass(frozen=True, slots=True)
class DaemonProxyDependencies:
    runtime_factory: Callable[[], CliRuntime]
    check_daemon_http_health: CheckDaemonHealth
    read_project_id: Callable[[], str | None]
    http_client_factory: Callable[[], httpx.AsyncClient]
    logger: logging.Logger


_MAX_INTENT_QUERY_CHARS = 1_024


def read_project_id() -> str | None:
    """Read project_id from the environment or nearest .gobby/project.json."""
    env_project_id = os.environ.get("GOBBY_PROJECT_ID")
    if env_project_id:
        return env_project_id

    for root in [Path.cwd(), *Path.cwd().parents]:
        project_file = root / ".gobby" / "project.json"
        if not project_file.exists():
            continue
        try:
            data = json.loads(project_file.read_text())
        except (PermissionError, json.JSONDecodeError, OSError):
            return None
        project_id = data.get("id")
        return project_id if isinstance(project_id, str) else None
    return None


def default_daemon_proxy_dependencies() -> DaemonProxyDependencies:
    return DaemonProxyDependencies(
        runtime_factory=lambda: CliRuntime(None),
        check_daemon_http_health=_check_daemon_http_health,
        read_project_id=read_project_id,
        http_client_factory=httpx.AsyncClient,
        logger=logging.getLogger("gobby.mcp.stdio"),
    )


class DaemonProxy:
    """Proxy for HTTP daemon API calls."""

    def __init__(
        self,
        port: int,
        deps_factory: Callable[[], DaemonProxyDependencies] | None = None,
    ):
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._deps_factory = deps_factory or default_daemon_proxy_dependencies
        self._project_id: str | None = self._deps_factory().read_project_id()
        self._environment_session_id: str | None = os.environ.get("GOBBY_SESSION_ID") or None
        self._terminal_context_header = serialize_terminal_context(current_terminal_context())
        self._last_health_ok_at = 0.0
        self._auth_headers = daemon_auth_headers()
        self._client: httpx.AsyncClient | None = None
        self._tool_timeouts: dict[str, float] | None = None
        self._tool_timeouts_lock = asyncio.Lock()

    async def _get_tool_timeouts(self) -> dict[str, float]:
        """Cache the configured tool-timeout map after the first read attempt."""
        if self._tool_timeouts is not None:
            return self._tool_timeouts
        async with self._tool_timeouts_lock:
            if self._tool_timeouts is not None:
                return self._tool_timeouts

            def read() -> dict[str, float]:
                deps = self._deps_factory()
                runtime = deps.runtime_factory()
                try:
                    config = runtime.require_config(apply_migrations=False)
                    return dict(config.mcp_client_proxy.tool_timeouts)
                finally:
                    runtime.close()

            try:
                self._tool_timeouts = await asyncio.to_thread(read)
            except Exception as exc:
                self._deps_factory().logger.warning(
                    "Failed to capture MCP tool timeout configuration: %s", exc
                )
                return {}
        return self._tool_timeouts

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._deps_factory().http_client_factory()
        return self._client

    async def aclose(self) -> None:
        """Close the reusable HTTP client owned by this proxy."""
        client = self._client
        if client is None:
            return
        await client.aclose()
        if self._client is client:
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        timeout: float = 30.0,
        project_id: str | None = None,
        session_id: str | None = None,
        params: dict[str, str] | None = None,
        preflight: bool = False,
    ) -> dict[str, Any]:
        """Make HTTP request to daemon."""
        if preflight:
            now = time.monotonic()
            if now - self._last_health_ok_at >= DAEMON_PROXY_PREFLIGHT_CACHE_SECONDS:
                if not await self._deps_factory().check_daemon_http_health(
                    self.port,
                    timeout=DAEMON_PROXY_PREFLIGHT_TIMEOUT_SECONDS,
                    base_url=self.base_url,
                ):
                    return _daemon_unavailable_result(
                        self.port,
                        "health check did not respond within "
                        f"{DAEMON_PROXY_PREFLIGHT_TIMEOUT_SECONDS:g}s",
                    )
                self._last_health_ok_at = time.monotonic()

        headers: dict[str, str] = {
            MCP_WRAPPER_PROTOCOL_VERSION_HEADER: MCP_WRAPPER_PROTOCOL_VERSION,
        }
        effective_project_id = project_id or self._project_id
        caller_project_id = self._project_id
        managed_run_id = os.environ.get("GOBBY_AGENT_RUN_ID")
        effective_session_id: str | None
        if managed_run_id and self._environment_session_id:
            # A managed agent's caller identity is pinned by its spawn env:
            # the environment value is the resolved session UUID, while
            # per-call refs like "#42" cannot be resolved client-side and the
            # run capability only ever authenticates as its own session.
            effective_session_id = self._environment_session_id
        else:
            effective_session_id = session_id or self._environment_session_id
        if effective_project_id:
            headers["X-Gobby-Project-Id"] = effective_project_id
        if caller_project_id:
            headers["X-Gobby-Caller-Project-Id"] = caller_project_id
        if effective_session_id:
            headers["X-Gobby-Session-Id"] = effective_session_id
        else:
            headers[TERMINAL_CONTEXT_HEADER] = self._terminal_context_header
        if managed_run_id:
            headers[AGENT_RUN_ID_HEADER] = managed_run_id

        try:
            client = self._get_client()
            request_headers = {**headers, **self._auth_headers}
            request_kwargs: dict[str, Any] = {
                "json": json,
                "headers": request_headers,
                "timeout": timeout,
            }
            if params:
                request_kwargs["params"] = params
            resp = await client.request(
                method,
                f"{self.base_url}{path}",
                **request_kwargs,
            )
            if resp.status_code == 401:
                self._auth_headers = daemon_auth_headers()
                retry_headers = {**headers, **self._auth_headers}
                request_kwargs["headers"] = retry_headers
                resp = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    **request_kwargs,
                )
            if resp.status_code == 200:
                data: dict[str, Any] = resp.json()
                return data
            if resp.status_code == 409:
                try:
                    error_data = resp.json()
                except ValueError:
                    error_data = None
                if isinstance(error_data, dict):
                    detail = error_data.get("detail")
                    if (
                        isinstance(detail, dict)
                        and detail.get("error_code") == ToolProxyErrorCode.SESSION_REQUIRED.value
                    ):
                        return detail
            return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}
        except httpx.ConnectError:
            return _daemon_unavailable_result(self.port, "connection failed")
        except httpx.TimeoutException:
            return _request_timeout_result(path, timeout)
        except Exception as e:
            error_msg = str(e) or f"{type(e).__name__}: (no message)"
            return {"success": False, "error": error_msg}

    async def get_status(self, session_id: str | None = None) -> dict[str, Any]:
        """Get daemon status."""
        return await self._request("GET", "/api/admin/status", session_id=session_id)

    async def list_tools(
        self,
        server_name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """List tools from MCP servers."""
        if server_name:
            listing_response: dict[str, Any] = _strip_none(
                await self._request(
                    "GET",
                    f"/api/mcp/{server_name}/tools",
                    session_id=session_id,
                )
            )
            return listing_response
        status = await self.get_status(session_id=session_id)
        if status.get("success") is False:
            return status
        servers = status.get("mcp_servers", {})
        all_tools: dict[str, list[dict[str, Any]]] = {}
        for srv_name in servers:
            result = await self._request(
                "GET",
                f"/api/mcp/{srv_name}/tools",
                session_id=session_id,
            )
            if result.get("success"):
                all_tools[srv_name] = result.get("tools", [])
        listing_response = _strip_none(
            {
                "success": True,
                "servers": [{"name": n, "tools": t} for n, t in all_tools.items()],
            }
        )
        return listing_response

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: str | dict[str, Any] | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        intent: str | None = None,
        preflight_enabled: bool = True,
    ) -> dict[str, Any]:
        if server_name == "gobby-workflows" and tool_name == REMOVED_WORKFLOW_WAIT_TOOL:
            return _removed_wait_for_completion_result()

        tool_timeouts = await self._get_tool_timeouts()

        timeout = 30.0
        if tool_timeouts and tool_name in tool_timeouts:
            timeout = tool_timeouts[tool_name]
        elif tool_name in EXTENDED_TIMEOUT_TOOL_NAMES:
            timeout = 300.0
        elif tool_name in WAIT_TOOL_NAMES:
            arg_map = arguments if isinstance(arguments, dict) else {}
            raw_timeout = arg_map.get("timeout")
            if raw_timeout is None:
                raw_timeout = arg_map.get("timeout_seconds", 300.0)
            try:
                arg_timeout = float(raw_timeout)
            except (TypeError, ValueError):
                arg_timeout = 300.0
            if not math.isfinite(arg_timeout) or arg_timeout <= 0:
                return {
                    "success": False,
                    "error": f"Invalid wait timeout: {raw_timeout!r}",
                }
            timeout = arg_timeout + WAIT_TOOL_HTTP_TIMEOUT_BUFFER_SECONDS

        request_path = f"/api/mcp/{server_name}/tools/{tool_name}"
        request_payload: Any = arguments if arguments is not None else {}
        if tool_name in WAIT_TOOL_NAMES:
            request_path = "/api/mcp/tools/call"
            request_payload = {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments if arguments is not None else {},
            }
            if intent:
                request_payload["intent"] = intent

        request_kwargs: dict[str, Any] = {
            "json": request_payload,
            "timeout": timeout,
        }
        if project_id:
            request_kwargs["project_id"] = project_id
        if session_id:
            request_kwargs["session_id"] = session_id
        if intent and tool_name not in WAIT_TOOL_NAMES:
            request_kwargs["params"] = {"intent": intent[:_MAX_INTENT_QUERY_CHARS]}
        return await self._request(
            "POST",
            request_path,
            **request_kwargs,
            preflight=preflight_enabled,
        )

    async def get_tool_schema(
        self,
        server_name: str,
        tool_name: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Get schema for a specific tool."""
        result = await self._request(
            "POST",
            "/api/mcp/tools/schema",
            json={"server_name": server_name, "tool_name": tool_name},
            session_id=session_id,
        )
        if "error" in result:
            return {"success": False, "error": result["error"]}
        schema_response: dict[str, Any] = _strip_none(
            {
                "success": True,
                "tool": {
                    "name": result.get("name"),
                    "description": result.get("description"),
                    "inputSchema": result.get("inputSchema"),
                },
            }
        )
        return schema_response

    async def list_mcp_servers(self, session_id: str | None = None) -> dict[str, Any]:
        """List configured MCP servers (includes internal gobby-* servers)."""
        result = await self._request("GET", "/api/mcp/servers", session_id=session_id)
        return compact_mcp_server_list(result)

    async def recommend_tools(
        self,
        task_description: str,
        agent_id: str | None = None,
        search_mode: str = "llm",
        top_k: int = 10,
        min_similarity: float = 0.3,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Get tool recommendations for a task."""
        return await self._request(
            "POST",
            "/api/mcp/tools/recommend",
            json={
                "task_description": task_description,
                "agent_id": agent_id,
                "search_mode": search_mode,
                "top_k": top_k,
                "min_similarity": min_similarity,
                "cwd": cwd,
            },
            timeout=60.0,
        )

    async def search_tools(
        self,
        query: str,
        top_k: int = 10,
        min_similarity: float = 0.0,
        server_name: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Search for tools using semantic similarity."""
        return await self._request(
            "POST",
            "/api/mcp/tools/search",
            json={
                "query": query,
                "top_k": top_k,
                "min_similarity": min_similarity,
                "server": server_name,
                "cwd": cwd,
            },
            timeout=60.0,
        )

    async def add_mcp_server(
        self,
        name: str,
        transport: str,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Add a new MCP server to the daemon's configuration."""
        return await self._request(
            "POST",
            "/api/mcp/servers",
            json={
                "name": name,
                "transport": transport,
                "url": url,
                "headers": headers,
                "command": command,
                "args": args,
                "env": env,
                "enabled": enabled,
            },
        )

    async def remove_mcp_server(self, name: str) -> dict[str, Any]:
        """Remove an MCP server from the daemon's configuration."""
        return await self._request("DELETE", f"/api/mcp/servers/{name}")

    async def import_mcp_server(
        self,
        from_project: str | None = None,
        servers: list[str] | None = None,
        github_url: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Import MCP servers from various sources."""
        return await self._request(
            "POST",
            "/api/mcp/servers/import",
            json={
                "from_project": from_project,
                "servers": servers,
                "github_url": github_url,
                "query": query,
            },
        )

    async def set_variable(
        self,
        name: str,
        value: str | int | float | bool | list[Any] | dict[str, Any] | None,
        session_id: str,
    ) -> dict[str, Any]:
        """Set a session-scoped variable."""
        return await self._request(
            "POST",
            f"/api/sessions/{quote(session_id, safe='')}/variables/set",
            json={"name": name, "value": value, "scope": "session"},
            session_id=session_id,
        )

    async def get_variable(
        self,
        name: str | None = None,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        """Get session-scoped variable(s)."""
        return await self._request(
            "POST",
            f"/api/sessions/{quote(session_id, safe='')}/variables/get",
            json={"name": name, "scope": "session"},
            session_id=session_id,
        )

    async def init_project(self, name: str, project_path: str | None = None) -> dict[str, Any]:
        """Initialize a new Gobby project.

        Note: Project initialization requires CLI access and cannot be done
        via the MCP proxy. Use 'gobby init' command instead.
        """
        return {
            "success": False,
            "error": "Project initialization requires CLI access. Use 'gobby init' command instead.",
        }
