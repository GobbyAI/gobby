"""
Gobby Daemon Tools MCP Server.
"""

import json
import logging
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy._coerce_arguments import coerce_string_arguments
from gobby.mcp_proxy.instructions import build_gobby_instructions
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.services.recommendation import RecommendationService, SearchMode
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.utils import project_context as project_context_utils

logger = logging.getLogger("gobby.mcp.server")


class GobbyDaemonTools:
    """Handler for Gobby Daemon MCP tools (Refactored to use services)."""

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        daemon_port: int,
        websocket_port: int,
        start_time: float,
        internal_manager: Any,
        config: DaemonConfig | None = None,
        llm_service: Any | None = None,
        session_manager: Any | None = None,
        memory_manager: Any | None = None,
        config_manager: Any | None = None,
        semantic_search: Any | None = None,
        fallback_resolver: Any | None = None,
        hook_manager_resolver: Callable[[], Any | None] | None = None,
    ):
        self.config = config
        self.internal_manager = internal_manager
        self._mcp_manager = mcp_manager  # Store for project_id access
        self._semantic_search = semantic_search  # Store for direct search access
        self._session_manager = session_manager  # Store for per-call project resolution
        self.daemon_port = daemon_port
        self.websocket_port = websocket_port
        self.start_time = start_time

        # Initialize services
        self.tool_proxy = ToolProxyService(
            mcp_manager,
            internal_manager=internal_manager,
            fallback_resolver=fallback_resolver,
            hook_manager_resolver=hook_manager_resolver,
        )
        self.server_mgmt = ServerManagementService(
            mcp_manager, config_manager, config, llm_service=llm_service
        )
        self.recommendation = RecommendationService(
            llm_service,
            mcp_manager,
            semantic_search=semantic_search,
            project_id=None,  # Resolved per-call via get_project_context()
            config=config.recommend_tools if config else None,
        )

    # --- System Tools ---

    async def status(self) -> dict[str, Any]:
        """Get the current status of the Gobby daemon."""
        import time

        uptime = time.time() - self.start_time
        return {
            "success": True,
            "running": True,
            "healthy": True,
            "http_port": self.daemon_port,
            "websocket_port": self.websocket_port,
            "uptime_seconds": round(uptime, 2),
        }

    async def list_mcp_servers(self, name_filter: str | None = None) -> dict[str, Any]:
        """List configured MCP servers.

        Args:
            name_filter: Optional glob pattern to filter server names (e.g., "gobby-*").
        """
        import fnmatch

        server_list: list[dict[str, Any]] = []
        connected_count = 0

        # Internal servers (always connected)
        if self.internal_manager:
            for registry in self.internal_manager.get_all_registries():
                server_list.append(
                    {"name": registry.name, "state": "connected", "transport": "internal"}
                )
                connected_count += 1

        # External servers
        mgr = self._mcp_manager
        for config in mgr.server_configs:
            health = mgr.health.get(config.name)
            state = health.state.value if health else "unknown"
            is_connected = config.name in mgr.connections
            if is_connected:
                connected_count += 1
            entry: dict[str, Any] = {
                "name": config.name,
                "state": state,
                "transport": config.transport,
            }
            if not config.enabled:
                entry["enabled"] = False
            server_list.append(entry)

        # Apply name filter if provided
        if name_filter:
            server_list = [s for s in server_list if fnmatch.fnmatch(s["name"], name_filter)]
            connected_count = sum(1 for s in server_list if s.get("state") == "connected")

        return {
            "success": True,
            "servers": server_list,
            "total": len(server_list),
            "connected": connected_count,
        }

    # --- Tool Proxying ---

    def _resolve_and_set_project_context(self, session_id: str) -> Any:
        """Look up session's project_id and set context var for this call."""
        from gobby.utils.project_context import set_project_context_from_session

        if not self._session_manager:
            return None

        try:
            return set_project_context_from_session(
                session_id, self._session_manager, self._session_manager.db
            )
        except Exception as e:
            logger.debug(f"Failed to set project context for session {session_id}: {e}")
            return None

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: str | dict[str, Any] | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> Any:
        """Call a tool.

        Returns the tool result, or a CallToolResult with isError=True if the
        underlying service indicates an error. This ensures the MCP protocol
        properly signals errors to LLM clients instead of returning error dicts
        as successful responses.

        When session_id is provided and a workflow is active, checks that the
        tool is not blocked by the current workflow step's blocked_tools setting.

        Args:
            server_name: Target MCP server name.
            tool_name: Tool to call on the server.
            arguments: Tool arguments (dict or JSON string).
            session_id: Session ID for context resolution and workflow checks.
            project_id: Optional project UUID or name. When provided, overrides
                session-derived project context, enabling cross-project tool
                operations (e.g., an agent in project A creating a task in
                project B).
        """
        # Set project + session context for this call.
        # Priority: explicit project_id > session-derived project > no context.
        # Session context (conversation tracking) is always from session_id.
        project_token = None
        session_token = None
        if project_id:
            if self._session_manager and self._session_manager.db:
                project_token = project_context_utils.set_project_context_from_ref(
                    project_id, self._session_manager.db
                )
                if project_token is None:
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text=f"Error: project_id '{project_id}' not found. "
                                "Use a valid project UUID or name.",
                            )
                        ],
                        isError=True,
                    )
            else:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text="Error: project_id provided but no database available "
                            "to resolve it.",
                        )
                    ],
                    isError=True,
                )
        elif session_id:
            project_token = self._resolve_and_set_project_context(session_id)

        if session_id:
            conversation_id = None
            if self._session_manager:
                session = self._session_manager.get(session_id)
                if session:
                    conversation_id = session.external_id
            # Set session context ContextVar — tools read from this directly
            from gobby.utils.session_context import SessionContext, set_session_context

            session_token = set_session_context(
                SessionContext(session_id=session_id, conversation_id=conversation_id)
            )
        # Coerce string arguments to dict (agents often stringify JSON,
        # sometimes with literal \" escapes instead of proper quotes)
        if isinstance(arguments, str):
            parsed = coerce_string_arguments(arguments)
            if parsed is not None:
                arguments = parsed
            else:
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=f"Error: 'arguments' must be a JSON object, got invalid string: {str(arguments)[:200]}",
                        )
                    ],
                    isError=True,
                )

        # At this point arguments is dict or None (str case handled above)
        # Strip call_tool's own parameters that LLMs sometimes flatten into
        # the arguments dict instead of passing as separate parameters.
        effective_arguments: dict[str, Any] | None = None
        if isinstance(arguments, dict):
            effective_arguments = dict(arguments)  # Shallow copy to avoid modifying original
            for leaked_key in ("server_name", "tool_name", "project_id"):
                effective_arguments.pop(leaked_key, None)

        try:
            result = await self.tool_proxy.call_tool(
                server_name, tool_name, effective_arguments, session_id
            )
        finally:
            if project_token is not None:
                from gobby.utils.project_context import reset_project_context

                reset_project_context(project_token)
            if session_token is not None:
                from gobby.utils.session_context import reset_session_context

                reset_session_context(session_token)

        # Check if result indicates an error:
        # - Old pattern: {"success": False, "error": ...}
        # - New pattern: {"error": ...} (no success field)
        if isinstance(result, dict):
            is_error = result.get("success") is False or (
                "error" in result and "success" not in result
            )
            if is_error:
                # Build helpful error message with schema hint if available
                error_msg = result.get("error", "Unknown error")
                hint = result.get("hint", "")
                schema = result.get("schema")

                parts = [f"Error: {error_msg}"]
                if hint:
                    parts.append(f"\n{hint}")
                if schema:
                    parts.append(f"\nCorrect schema:\n{json.dumps(schema, indent=2)}")

                # Return MCP error response with isError=True
                return CallToolResult(
                    content=[TextContent(type="text", text="\n".join(parts))],
                    isError=True,
                )

            # Strip redundant success field from successful responses
            if "success" in result:
                result = {k: v for k, v in result.items() if k != "success"}

        return result

    async def list_tools(self, server_name: str, session_id: str | None = None) -> dict[str, Any]:
        """List tools for a specific server, optionally filtered by workflow phase restrictions."""
        return await self.tool_proxy.list_tools(server_name, session_id=session_id)

    async def get_tool_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get tool schema."""
        return await self.tool_proxy.get_tool_schema(server_name, tool_name)

    async def read_mcp_resource(self, server_name: str, resource_uri: str) -> Any:
        """Read resource."""
        return await self.tool_proxy.read_resource(server_name, resource_uri)

    # --- Server Management ---

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
        """Add server."""
        return await self.server_mgmt.add_server(
            name, transport, url, command, args, env, headers, enabled
        )

    async def remove_mcp_server(self, name: str) -> dict[str, Any]:
        """Remove server."""
        return await self.server_mgmt.remove_server(name)

    async def import_mcp_server(
        self,
        from_project: str | None = None,
        servers: list[str] | None = None,
        github_url: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Import server."""
        return await self.server_mgmt.import_server(from_project, github_url, query, servers)

    # --- Recommendation ---

    async def recommend_tools(
        self,
        task_description: str,
        agent_id: str | None = None,
        search_mode: SearchMode = "llm",
        top_k: int = 10,
        min_similarity: float = 0.3,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Recommend tools for a task.

        Args:
            task_description: What the user wants to accomplish
            agent_id: Optional agent profile for filtering (reserved)
            search_mode: How to search - "llm" (default), "semantic", or "hybrid"
            top_k: Maximum recommendations to return (semantic/hybrid modes)
            min_similarity: Minimum similarity threshold (semantic/hybrid modes)
            project_id: Project ID for semantic/hybrid search

        Returns:
            Dict with tool recommendations
        """
        return await self.recommendation.recommend_tools(
            task_description,
            agent_id=agent_id,
            search_mode=search_mode,
            top_k=top_k,
            min_similarity=min_similarity,
            project_id=project_id,
        )

    # --- Semantic Search ---

    async def search_tools(
        self,
        query: str,
        top_k: int = 10,
        min_similarity: float = 0.0,
        server_name: str | None = None,
    ) -> dict[str, Any]:
        """Search for tools using semantic similarity.

        Args:
            query: Natural language query describing the tool you need
            top_k: Maximum number of results to return (default: 10)
            min_similarity: Minimum similarity threshold (default: 0.0)
            server_name: Optional server name to filter results

        Returns:
            Dict with search results and metadata
        """
        if not self._semantic_search:
            return {
                "success": False,
                "error": "Semantic search not configured",
                "query": query,
            }

        project_id = self._mcp_manager.project_id
        if not project_id:
            ctx = project_context_utils.get_project_context()
            project_id = ctx.get("id") if ctx else None
        if not project_id:
            return {
                "success": False,
                "error": "No project_id available. Run 'gobby init' first.",
                "query": query,
            }

        try:
            results = await self._semantic_search.search_tools(
                query=query,
                project_id=project_id,
                top_k=top_k,
                min_similarity=min_similarity,
                server_filter=server_name,
            )

            return {
                "success": True,
                "query": query,
                "results": [r.to_dict() for r in results],
                "total_results": len(results),
            }
        except Exception as e:
            logger.error(f"Semantic search failed: {e}")
            return {"success": False, "error": str(e), "query": query}

    # --- Session Variables ---

    async def set_variable(
        self,
        name: str,
        value: str | int | float | bool | list[Any] | dict[str, Any] | None,
        session_id: Annotated[
            str,
            Field(
                description="Your Gobby Session ID (e.g. #3439). Use the value from 'Gobby Session ID: #N' in your system prompt."
            ),
        ],
    ) -> dict[str, Any]:
        """Set a variable. Session-scoped by default. Pass workflow param to scope to a specific workflow instance."""
        if not self._session_manager or not self._session_manager.db:
            return {"success": False, "error": "Session manager not available"}

        from gobby.mcp_proxy.tools.workflows._variables import set_variable as _set_var

        return _set_var(
            self._session_manager,
            self._session_manager.db,
            name,
            value,
            session_id,
            workflow=None,
        )

    async def get_variable(
        self,
        name: str | None = None,
        *,
        session_id: Annotated[
            str,
            Field(
                description="Your Gobby Session ID (e.g. #3439). Use the value from 'Gobby Session ID: #N' in your system prompt."
            ),
        ],
    ) -> dict[str, Any]:
        """Get a variable (or all variables). Session-scoped by default. Pass workflow param to read from a specific workflow instance."""
        if not self._session_manager or not self._session_manager.db:
            return {"success": False, "error": "Session manager not available"}

        from gobby.mcp_proxy.tools.workflows._variables import get_variable as _get_var

        return _get_var(
            self._session_manager,
            self._session_manager.db,
            name,
            session_id,
            workflow=None,
        )

    # Hook Extension tools migrated to gobby-plugins internal registry
    # (see src/gobby/mcp_proxy/tools/plugins/)


def create_mcp_server(tools_handler: GobbyDaemonTools) -> FastMCP:
    """Create the FastMCP server instance for the HTTP daemon."""
    mcp = FastMCP("gobby", instructions=build_gobby_instructions())

    # System tools
    mcp.add_tool(tools_handler.status)
    mcp.add_tool(tools_handler.list_mcp_servers)

    # Tool Proxy
    mcp.add_tool(tools_handler.call_tool)
    mcp.add_tool(tools_handler.list_tools)
    mcp.add_tool(tools_handler.get_tool_schema)
    # read_mcp_resource is a tool that proxies resource reading
    mcp.add_tool(tools_handler.read_mcp_resource)

    # Server Management
    mcp.add_tool(tools_handler.add_mcp_server)
    mcp.add_tool(tools_handler.remove_mcp_server)
    mcp.add_tool(tools_handler.import_mcp_server)

    # Recommendation
    mcp.add_tool(tools_handler.recommend_tools)

    # Semantic Search
    mcp.add_tool(tools_handler.search_tools)

    # Session Variables
    mcp.add_tool(tools_handler.set_variable)
    mcp.add_tool(tools_handler.get_variable)

    # Hook Extension tools are now in gobby-plugins internal registry

    return mcp
