"""
Stdio MCP server implementation.

This server runs as a stdio process for Claude Code and proxies
tool calls to the HTTP daemon.
"""

import asyncio
import logging
import sys
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from gobby.config.app import load_config
from gobby.mcp_proxy.daemon_control import (
    check_daemon_http_health,
    get_daemon_pid,
    is_daemon_running,
    restart_daemon_process,
    start_daemon_process,
    stop_daemon_process,
)
from gobby.mcp_proxy.instructions import build_gobby_instructions
from gobby.mcp_proxy.registries import setup_internal_registries

LLM_TASK_TOOLS = (
    "close_task",
    "expand_task",
    "apply_tdd",
    "suggest_next_task",
    "validate_task",
    "validate_and_fix",
    "run_fix_attempt",
)


__all__ = [
    "create_stdio_mcp_server",
    "check_daemon_http_health",
    "get_daemon_pid",
    "is_daemon_running",
    "restart_daemon_process",
    "start_daemon_process",
    "stop_daemon_process",
]

logger = logging.getLogger("gobby.mcp.stdio")


class DaemonProxy:
    """Proxy for HTTP daemon API calls."""

    def __init__(self, port: int):
        self.port = port
        self.base_url = f"http://localhost:{port}"

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Make HTTP request to daemon."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=json,
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    data: dict[str, Any] = resp.json()
                    return data
                else:
                    return {"success": False, "error": f"HTTP {resp.status_code}: {resp.text}"}
        except httpx.ConnectError:
            return {"success": False, "error": "Daemon not running or not reachable"}
        except Exception as e:
            error_msg = str(e) or f"{type(e).__name__}: (no message)"
            return {"success": False, "error": error_msg}

    async def get_status(self) -> dict[str, Any]:
        """Get daemon status."""
        return await self._request("GET", "/api/admin/status")

    async def list_tools(self, server_name: str | None = None) -> dict[str, Any]:
        """List tools from MCP servers."""
        if server_name:
            return await self._request("GET", f"/api/mcp/{server_name}/tools")
        # List all - need to get server list first
        status = await self.get_status()
        if status.get("success") is False:
            return status
        servers = status.get("mcp_servers", {})
        all_tools: dict[str, list[dict[str, Any]]] = {}
        for srv_name in servers:
            result = await self._request("GET", f"/api/mcp/{srv_name}/tools")
            if result.get("success"):
                all_tools[srv_name] = result.get("tools", [])
        return {
            "success": True,
            "servers": [{"name": n, "tools": t} for n, t in all_tools.items()],
        }

    async def call_tool(
        self, server_name: str, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # Tool-specific timeouts
        config = load_config()
        # Default to standard timeout
        timeout = 30.0
        # Check for tool-specific override in config
        if (
            config.mcp_client_proxy.tool_timeouts
            and tool_name in config.mcp_client_proxy.tool_timeouts
        ):
            timeout = config.mcp_client_proxy.tool_timeouts[tool_name]
        # Fallback for LLM-based task tools if not explicit in config
        elif tool_name in LLM_TASK_TOOLS:
            timeout = 300.0
        # Wait tools: use the requested timeout plus a buffer
        elif tool_name in (
            "wait_for_task",
            "wait_for_any_task",
            "wait_for_all_tasks",
            "wait_for_completion",
            "wait_for_agent",
        ):
            # Extract timeout from arguments, default to 300s if not specified
            arg_timeout = float((arguments or {}).get("timeout", 300.0))
            # Add 30s buffer for HTTP overhead
            timeout = arg_timeout + 30.0
        return await self._request(
            "POST",
            f"/api/mcp/{server_name}/tools/{tool_name}",
            json=arguments or {},
            timeout=timeout,
        )

    async def get_tool_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get schema for a specific tool."""
        result = await self._request(
            "POST",
            "/api/mcp/tools/schema",
            json={"server_name": server_name, "tool_name": tool_name},
        )
        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {
            "success": True,
            "tool": {
                "name": result.get("name"),
                "description": result.get("description"),
                "inputSchema": result.get("inputSchema"),
            },
        }

    async def list_mcp_servers(self) -> dict[str, Any]:
        """List configured MCP servers (includes internal gobby-* servers)."""
        return await self._request("GET", "/api/mcp/servers")

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
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Set a session-scoped variable."""
        return await self._request(
            "POST",
            "/api/workflows/variables/set",
            json={"name": name, "value": value, "session_id": session_id},
        )

    async def get_variable(
        self,
        name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Get session-scoped variable(s)."""
        return await self._request(
            "POST",
            "/api/workflows/variables/get",
            json={"name": name, "session_id": session_id},
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


def create_stdio_mcp_server() -> FastMCP:
    """Create stdio MCP server."""
    # Load configuration
    config = load_config()

    # Initialize basic managers (mocked/simplified for this refactor example)
    session_manager = None
    memory_manager = None

    # Setup internal registries using extracted function
    _ = setup_internal_registries(config, session_manager, memory_manager)

    # Initialize MCP server and daemon proxy
    mcp = FastMCP("gobby", instructions=build_gobby_instructions())
    proxy = DaemonProxy(config.daemon_port)

    register_proxy_tools(mcp, proxy)

    return mcp


def register_proxy_tools(mcp: FastMCP, proxy: DaemonProxy) -> None:
    """Register proxy tools on the MCP server."""

    @mcp.tool()
    async def list_mcp_servers() -> dict[str, Any]:
        """
        List all MCP servers configured in the daemon.

        Returns details about each MCP server including connection status,
        available tools, and resources.

        Returns:
            Dict with servers list, total count, and connected count
        """
        return await proxy.list_mcp_servers()

    @mcp.tool()
    async def list_tools(server_name: str) -> dict[str, Any]:
        """
        List tools from MCP servers.

        Use this to discover tools available on servers.

        Args:
            server_name: Server name (e.g., "context7", "supabase").
                   Use list_mcp_servers() first to discover available servers.

        Returns:
            Dict with tool listings
        """
        return await proxy.list_tools(server_name)

    @mcp.tool()
    async def get_tool_schema(server_name: str, tool_name: str) -> dict[str, Any]:
        """
        Get full schema (inputSchema) for a specific MCP tool.

        Use list_tools() first to discover available tools, then use this to get
        full details before calling the tool.

        Args:
            server_name: Name of the MCP server (e.g., "context7", "supabase")
            tool_name: Name of the tool (e.g., "get-library-docs", "list_tables")

        Returns:
            Dict with tool name, description, and full inputSchema
        """
        return await proxy.get_tool_schema(server_name, tool_name)

    @mcp.tool()
    async def call_tool(
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a tool on a connected MCP server.

        This is the primary way to interact with MCP servers (Supabase, memory, etc.)
        through the Gobby daemon.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the specific tool to execute
            arguments: Dictionary of arguments required by the tool (optional)

        Returns:
            Dictionary with success status and tool execution result
        """
        return await proxy.call_tool(server_name, tool_name, arguments)

    @mcp.tool()
    async def recommend_tools(
        task_description: str,
        agent_id: str | None = None,
        search_mode: str = "llm",
        top_k: int = 10,
        min_similarity: float = 0.3,
    ) -> dict[str, Any]:
        """
        Get intelligent tool recommendations for a given task.

        Args:
            task_description: Description of what you're trying to accomplish
            agent_id: Optional agent profile ID to filter tools by assigned permissions
            search_mode: How to search - "llm" (default), "semantic", or "hybrid"
            top_k: Maximum recommendations to return (semantic/hybrid modes)
            min_similarity: Minimum similarity threshold (semantic/hybrid modes)

        Returns:
            Dict with tool recommendations and usage suggestions
        """
        import os

        cwd = os.getcwd()
        return await proxy.recommend_tools(
            task_description,
            agent_id,
            search_mode=search_mode,
            top_k=top_k,
            min_similarity=min_similarity,
            cwd=cwd,
        )

    @mcp.tool()
    async def search_tools(
        query: str,
        top_k: int = 10,
        min_similarity: float = 0.0,
        server_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Search for tools using semantic similarity.

        Uses embedding-based search to find tools matching a natural language query.
        Requires embeddings to be generated first (happens automatically on first search).

        Args:
            query: Natural language description of the tool you need
            top_k: Maximum number of results to return (default: 10)
            min_similarity: Minimum similarity threshold 0-1 (default: 0.0)
            server_name: Optional server name to filter results

        Returns:
            Dict with matching tools sorted by similarity
        """
        import os

        cwd = os.getcwd()
        return await proxy.search_tools(
            query,
            top_k=top_k,
            min_similarity=min_similarity,
            server_name=server_name,
            cwd=cwd,
        )

    @mcp.tool()
    async def add_mcp_server(
        name: str,
        transport: str,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """
        Add a new MCP server to the daemon's configuration.

        Args:
            name: Unique server name
            transport: Transport type - "http", "stdio", or "websocket"
            url: Server URL (required for http/websocket)
            headers: Custom HTTP headers (optional)
            command: Command to run (required for stdio)
            args: Command arguments (optional for stdio)
            env: Environment variables (optional for stdio)
            enabled: Whether server is enabled (default: True)

        Returns:
            Result dict with success status
        """
        return await proxy.add_mcp_server(
            name=name,
            transport=transport,
            url=url,
            headers=headers,
            command=command,
            args=args,
            env=env,
            enabled=enabled,
        )

    @mcp.tool()
    async def remove_mcp_server(name: str) -> dict[str, Any]:
        """
        Remove an MCP server from the daemon's configuration.

        Args:
            name: Server name to remove

        Returns:
            Result dict with success status
        """
        return await proxy.remove_mcp_server(name)

    @mcp.tool()
    async def import_mcp_server(
        from_project: str | None = None,
        servers: list[str] | None = None,
        github_url: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """
        Import MCP servers from various sources.

        Args:
            from_project: Source project name to import servers from
            servers: Optional list of specific server names to import
            github_url: GitHub repository URL to parse for MCP server config
            query: Natural language search query

        Returns:
            Result dict with imported servers or config to fill in
        """
        return await proxy.import_mcp_server(
            from_project=from_project,
            servers=servers,
            github_url=github_url,
            query=query,
        )

    @mcp.tool()
    async def init_project(
        name: str,
        project_path: str | None = None,
    ) -> dict[str, Any]:
        """
        Initialize a new Gobby project.

        Note: Project initialization requires CLI access and cannot be done
        via the MCP proxy. Use 'gobby init' command instead.

        Args:
            name: Project name
            project_path: Path to project directory (optional)

        Returns:
            Result dict with error (CLI access required)
        """
        return await proxy.init_project(name, project_path)

    @mcp.tool()
    async def set_variable(
        name: str,
        value: str | int | float | bool | None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Set a session-scoped variable. Top-level shortcut — no progressive discovery needed.

        Args:
            name: Variable name
            value: Variable value (string, number, boolean, or null)
            session_id: Session ID (accepts #N, N, UUID, or prefix)

        Returns:
            Dict with ok status and stored value
        """
        return await proxy.set_variable(name=name, value=value, session_id=session_id)

    @mcp.tool()
    async def get_variable(
        name: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Get session-scoped variable(s). Top-level shortcut — no progressive discovery needed.

        Args:
            name: Variable name (omit to get all variables)
            session_id: Session ID (accepts #N, N, UUID, or prefix)

        Returns:
            Dict with variable value(s)
        """
        return await proxy.get_variable(name=name, session_id=session_id)


async def ensure_daemon_running() -> None:
    """Ensure the Gobby daemon is running and healthy."""
    config = load_config()
    port = config.daemon_port
    ws_port = config.websocket.port

    # Check if running
    if is_daemon_running():
        # 3 attempts before concluding unhealthy (5s timeout each)
        for attempt in range(3):
            if await check_daemon_http_health(port):
                return
            if attempt < 2:
                logger.warning(
                    "Daemon health check failed (attempt %d/3), retrying in 5s...",
                    attempt + 1,
                )
                await asyncio.sleep(5)

        # 3 consecutive failures → restart
        logger.warning("Daemon running but unhealthy after 3 checks, restarting...")
        pid = get_daemon_pid()
        await restart_daemon_process(pid, port, ws_port)
    else:
        # Start
        result = await start_daemon_process(port, ws_port)
        if not result.get("success"):
            logger.error(
                "Failed to start daemon: %s (port=%d, ws_port=%d)",
                result.get("error", "unknown error"),
                port,
                ws_port,
            )
            sys.exit(1)

    # Wait for health
    last_health_response = None
    for _i in range(10):
        last_health_response = await check_daemon_http_health(port)
        if last_health_response:
            return
        await asyncio.sleep(1)

    # Health check timed out
    pid = get_daemon_pid()
    logger.error(
        "Daemon failed to become healthy after 10 attempts (pid=%s, port=%d, ws_port=%d, last_health=%s)",
        pid,
        port,
        ws_port,
        last_health_response,
    )
    sys.exit(1)


async def main() -> None:
    """Main entry point for stdio MCP server."""
    # Ensure daemon is running first
    await ensure_daemon_running()

    # Create and run the MCP server
    mcp = create_stdio_mcp_server()
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
