"""FastMCP tool registration for the stdio proxy."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from mcp.server.fastmcp import Context, FastMCP

from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
    CanonicalCallToolWrapper,
    canonicalize_call_tool_wrapper,
)
from gobby.mcp_proxy.stdio_proxy import DaemonProxy
from gobby.mcp_proxy.wait_tools import (
    call_with_wait_heartbeat,
    prepare_client_guard,
)


class CanonicalizeCallToolWrapper(Protocol):
    def __call__(
        self,
        *,
        server_name: str | None,
        tool_name: str | None,
        arguments: str | dict[str, Any] | None = None,
        args: str | dict[str, Any] | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        intent: str | None = None,
    ) -> CanonicalCallToolWrapper: ...


class CallWithWaitHeartbeat(Protocol):
    def __call__(
        self,
        tool_call: Awaitable[dict[str, Any]],
        *,
        ctx: Context[Any, Any, Any] | None,
        tool_name: str,
        timeout: float | None,
    ) -> Awaitable[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ToolRegistrationDependencies:
    canonicalize_call_tool_wrapper: CanonicalizeCallToolWrapper
    input_error_type: type[CallToolWrapperInputError]
    prepare_client_guard: Callable[..., Any]
    call_with_wait_heartbeat: CallWithWaitHeartbeat


def default_tool_registration_dependencies() -> ToolRegistrationDependencies:
    return ToolRegistrationDependencies(
        canonicalize_call_tool_wrapper=canonicalize_call_tool_wrapper,
        input_error_type=CallToolWrapperInputError,
        prepare_client_guard=prepare_client_guard,
        call_with_wait_heartbeat=call_with_wait_heartbeat,
    )


def register_proxy_tools(
    mcp: FastMCP,
    proxy: DaemonProxy,
    *,
    deps_factory: Callable[[], ToolRegistrationDependencies] | None = None,
) -> None:
    """Register proxy tools on the MCP server."""
    get_deps = deps_factory or default_tool_registration_dependencies

    @mcp.tool()
    async def list_mcp_servers(session_id: str | None = None) -> dict[str, Any]:
        """
        List all MCP servers configured in the daemon.

        Use this for unknown-server discovery or explicit registry inspection.
        Returns connection status, available tools, and resources.

        Returns:
            Dict with servers list, total count, and connected count
        """
        if session_id:
            return await proxy.list_mcp_servers(session_id=session_id)
        return await proxy.list_mcp_servers()

    @mcp.tool()
    async def list_tools(server_name: str, session_id: str | None = None) -> dict[str, Any]:
        """
        List tools from MCP servers.

        Use this when the tool name is unknown or for explicit inventory inspection.

        Args:
            server_name: Known server name (e.g., "context7", "supabase").

        Returns:
            Dict with tool listings
        """
        if session_id:
            return await proxy.list_tools(server_name, session_id=session_id)
        return await proxy.list_tools(server_name)

    @mcp.tool()
    async def get_tool_schema(
        server_name: str,
        tool_name: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Get full schema (inputSchema) for a specific MCP tool.

        Call this directly for a known unleased tool. Use list_tools only when
        the tool name itself is unknown.

        Args:
            server_name: Name of the MCP server (e.g., "context7", "supabase")
            tool_name: Name of the tool (e.g., "get-library-docs", "list_tables")

        Returns:
            Dict with tool name, description, and full inputSchema
        """
        if session_id:
            return await proxy.get_tool_schema(server_name, tool_name, session_id=session_id)
        return await proxy.get_tool_schema(server_name, tool_name)

    @mcp.tool()
    async def call_tool(
        server_name: str | None = None,
        tool_name: str | None = None,
        arguments: str | dict[str, Any] | None = None,
        args: str | dict[str, Any] | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        intent: str | None = None,
        preflight_enabled: bool = True,
        ctx: Context[Any, Any, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute a tool on a connected MCP server.

        This is the primary way to interact with MCP servers (Supabase, memory, etc.)
        through the Gobby daemon.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the specific tool to execute
            arguments: Dictionary of arguments required by the tool (optional)
            args: Alias for arguments. Accepts dict or JSON string. Use 'arguments' preferred.
            session_id: Wrapper context (accepts #N, N, UUID, or prefix).
                Propagated to the daemon via X-Gobby-Session-Id header and used
                for Gobby context/workflow resolution. Same-repo calls can rely
                on wrapper or ambient session context; if the target schema
                requires session_id, the resolved UUID is supplied to the target
                arguments before validation. Use arguments.session_id only to
                target a different session. Local #N refs resolve in the caller
                project; cross-project target sessions should use UUIDs.
            project_id: Optional project UUID or name for cross-project tool calls.
            preflight_enabled: Whether to perform daemon health preflight before proxying.

        Returns:
            Dictionary with success status and tool execution result
        """
        deps = get_deps()
        try:
            canonical = deps.canonicalize_call_tool_wrapper(
                server_name=server_name,
                tool_name=tool_name,
                arguments=arguments,
                args=args,
                session_id=session_id,
                project_id=project_id,
                intent=intent,
            )
        except deps.input_error_type as exc:
            return {"success": False, "error": str(exc)}

        server_name = canonical.server_name
        tool_name = canonical.tool_name
        final_args = canonical.arguments
        session_id = canonical.session_id
        project_id = canonical.project_id
        intent = canonical.intent

        if not server_name or not tool_name:
            return {
                "success": False,
                "error": "Missing required parameters: server_name, tool_name",
            }

        guard = deps.prepare_client_guard(tool_name=tool_name, arguments=final_args)
        final_args = guard.arguments

        call_kwargs: dict[str, Any] = {}
        if project_id:
            call_kwargs["project_id"] = project_id
        if session_id:
            call_kwargs["session_id"] = session_id
        if intent:
            call_kwargs["intent"] = intent

        result = await deps.call_with_wait_heartbeat(
            proxy.call_tool(
                server_name,
                tool_name,
                final_args,
                **call_kwargs,
                preflight_enabled=preflight_enabled,
            ),
            ctx=ctx,
            tool_name=tool_name,
            timeout=guard.timeout,
        )
        if guard.wait_timeout_capped:
            result["requested_timeout_seconds"] = guard.requested_timeout_seconds
            result["effective_timeout_seconds"] = guard.effective_timeout_seconds
            result["wait_timeout_capped_by_mcp_wrapper"] = True
        return result

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
            min_similarity: Minimum similarity threshold 0-1 (semantic/hybrid modes)

        Returns:
            Dict with tool recommendations and usage suggestions
        """
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
        value: str | int | float | bool | list[Any] | dict[str, Any] | None,
        session_id: str,
    ) -> dict[str, Any]:
        """
        Set a session-scoped variable. Top-level shortcut — no progressive discovery needed.

        Args:
            name: Variable name
            value: JSON-compatible variable value
            session_id: Session ID (accepts #N, N, UUID, or prefix)

        Returns:
            Dict with ok status and stored value
        """
        return await proxy.set_variable(name=name, value=value, session_id=session_id)

    @mcp.tool()
    async def get_variable(
        session_id: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """
        Get session-scoped variable(s). Top-level shortcut — no progressive discovery needed.

        Args:
            session_id: Session ID (accepts #N, N, UUID, or prefix)
            name: Variable name (omit to get all variables)

        Returns:
            Dict with variable value(s)
        """
        return await proxy.get_variable(name=name, session_id=session_id)
