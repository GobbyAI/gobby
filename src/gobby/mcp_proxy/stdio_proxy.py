"""HTTP daemon proxy used by the stdio MCP wrapper."""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from gobby.config.app import load_config as _load_config
from gobby.mcp_proxy.daemon_control import check_daemon_http_health as _check_daemon_http_health
from gobby.mcp_proxy.server_list import compact_mcp_server_list
from gobby.mcp_proxy.session_bootstrap import (
    read_project_id as _read_project_id,
)
from gobby.mcp_proxy.session_bootstrap import (
    resolve_session_id_from_terminal_context as _resolve_session_id_from_terminal_context,
)
from gobby.mcp_proxy.stdio_results import (
    DAEMON_PROXY_PREFLIGHT_CACHE_SECONDS,
    DAEMON_PROXY_PREFLIGHT_TIMEOUT_SECONDS,
    REMOVED_WORKFLOW_WAIT_TOOL,
    _daemon_unavailable_result,
    _removed_wait_for_completion_result,
    _request_timeout_result,
    _strip_none,
)
from gobby.mcp_proxy.wait_tools import (
    EXTENDED_TIMEOUT_TOOL_NAMES,
    MCP_WRAPPER_FINGERPRINT_HEADER,
    WAIT_TOOL_HTTP_TIMEOUT_BUFFER_SECONDS,
    WAIT_TOOL_NAMES,
    mcp_wrapper_process_fingerprint,
)
from gobby.utils.local_token import daemon_auth_headers


class CheckDaemonHealth(Protocol):
    def __call__(
        self,
        port: int,
        timeout: float = 5.0,
        *,
        base_url: str | None = None,
    ) -> Awaitable[bool]: ...


class ResolveSessionIdFromTerminalContext(Protocol):
    def __call__(self, base_url: str, project_id: str) -> Awaitable[str | None]: ...


@dataclass(frozen=True, slots=True)
class DaemonProxyDependencies:
    load_config: Callable[[], Any]
    check_daemon_http_health: CheckDaemonHealth
    read_project_id: Callable[[], str | None]
    resolve_session_id_from_terminal_context: ResolveSessionIdFromTerminalContext
    logger: logging.Logger


# Retry interval for session bootstrap lookups via find_by_terminal_context.
# A failed lookup (parent_pid mismatch, daemon busy, session not yet active)
# is retried after this delay rather than permanently giving up.
_BOOTSTRAP_RETRY_INTERVAL_SECONDS: float = 10.0


def default_daemon_proxy_dependencies() -> DaemonProxyDependencies:
    return DaemonProxyDependencies(
        load_config=_load_config,
        check_daemon_http_health=_check_daemon_http_health,
        read_project_id=_read_project_id,
        resolve_session_id_from_terminal_context=_resolve_session_id_from_terminal_context,
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
        self._session_id: str | None = os.environ.get("GOBBY_SESSION_ID") or None
        self._last_bootstrap_attempt_at: float = 0.0
        self._last_health_ok_at = 0.0
        self._auth_headers = daemon_auth_headers()

    async def _resolve_session_id(self) -> str | None:
        if self._session_id:
            return self._session_id
        if not self._project_id:
            return None
        now = time.monotonic()
        if now - self._last_bootstrap_attempt_at < _BOOTSTRAP_RETRY_INTERVAL_SECONDS:
            return self._session_id
        self._last_bootstrap_attempt_at = now
        resolved_session_id = await self._deps_factory().resolve_session_id_from_terminal_context(
            self.base_url,
            self._project_id,
        )
        if resolved_session_id is not None:
            self._session_id = resolved_session_id
        return self._session_id

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        timeout: float = 30.0,
        project_id: str | None = None,
        session_id: str | None = None,
        preflight: bool = False,
    ) -> dict[str, Any]:
        """Make HTTP request to daemon."""
        if session_id:
            self._session_id = session_id
            self._last_bootstrap_attempt_at = time.monotonic()

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
            MCP_WRAPPER_FINGERPRINT_HEADER: mcp_wrapper_process_fingerprint(),
        }
        effective_project_id = project_id or self._project_id
        caller_project_id = self._project_id
        effective_session_id = session_id or await self._resolve_session_id()
        if effective_project_id:
            headers["X-Gobby-Project-Id"] = effective_project_id
        if caller_project_id:
            headers["X-Gobby-Caller-Project-Id"] = caller_project_id
        if effective_session_id:
            headers["X-Gobby-Session-Id"] = effective_session_id

        try:
            async with httpx.AsyncClient() as client:
                request_headers = {**headers, **self._auth_headers}
                resp = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    headers=request_headers,
                    timeout=timeout,
                )
                if resp.status_code == 401:
                    self._auth_headers = daemon_auth_headers()
                    retry_headers = {**headers, **self._auth_headers}
                    resp = await client.request(
                        method,
                        f"{self.base_url}{path}",
                        json=json,
                        headers=retry_headers,
                        timeout=timeout,
                    )
                if resp.status_code == 200:
                    data: dict[str, Any] = resp.json()
                    return data
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
        preflight_enabled: bool = True,
    ) -> dict[str, Any]:
        if server_name == "gobby-workflows" and tool_name == REMOVED_WORKFLOW_WAIT_TOOL:
            return _removed_wait_for_completion_result()

        try:
            config = self._deps_factory().load_config()
            tool_timeouts = config.mcp_client_proxy.tool_timeouts
        except Exception as exc:
            self._deps_factory().logger.warning(
                f"Failed to load config for MCP tool timeout overrides: {exc}"
            )
            tool_timeouts = {}

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
                return {
                    "success": False,
                    "error": f"Invalid wait timeout: {raw_timeout!r}",
                }
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

        request_kwargs: dict[str, Any] = {
            "json": request_payload,
            "timeout": timeout,
        }
        if project_id:
            request_kwargs["project_id"] = project_id
        if session_id:
            request_kwargs["session_id"] = session_id
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
        value: str | int | float | bool | None,
        session_id: str,
    ) -> dict[str, Any]:
        """Set a session-scoped variable."""
        return await self._request(
            "POST",
            "/api/workflows/variables/set",
            json={"name": name, "value": value, "session_id": session_id},
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
            "/api/workflows/variables/get",
            json={"name": name, "session_id": session_id},
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
