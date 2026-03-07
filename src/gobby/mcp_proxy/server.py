"""
Gobby Daemon Tools MCP Server.
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.instructions import build_gobby_instructions
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.services.recommendation import RecommendationService, SearchMode
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.mcp_proxy.services.system import SystemService
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.utils.project_context import get_project_context

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
    ):
        self.config = config
        self.internal_manager = internal_manager
        self._mcp_manager = mcp_manager  # Store for project_id access
        self._semantic_search = semantic_search  # Store for direct search access
        self._session_manager = session_manager  # Store for per-call project resolution

        # Initialize services
        self.system_service = SystemService(mcp_manager, daemon_port, websocket_port, start_time)
        self.tool_proxy = ToolProxyService(
            mcp_manager,
            internal_manager=internal_manager,
            fallback_resolver=fallback_resolver,
        )
        self.server_mgmt = ServerManagementService(mcp_manager, config_manager, config)
        self.recommendation = RecommendationService(
            llm_service,
            mcp_manager,
            semantic_search=semantic_search,
            project_id=None,  # Resolved per-call via get_project_context()
            config=config.recommend_tools if config else None,
        )

    # --- System Tools ---

    async def status(self) -> dict[str, Any]:
        """Get daemon status."""
        return self.system_service.get_status()

    async def list_mcp_servers(self) -> dict[str, Any]:
        """List configured MCP servers."""
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

        return {
            "success": True,
            "servers": server_list,
            "total": len(server_list),
            "connected": connected_count,
        }

    # --- Tool Proxying ---

    def _resolve_and_set_project_context(self, session_id: str) -> Any:
        """Look up session's project_id and set context var for this call."""
        from gobby.utils.project_context import set_project_context

        if not self._session_manager:
            return None

        session = self._session_manager.get(session_id)
        if not session or not session.project_id:
            return None

        # Try to enrich with full project data from DB
        try:
            from gobby.storage.projects import LocalProjectManager

            pm = LocalProjectManager(self._session_manager.db)
            project = pm.get(session.project_id)
            if project:
                ctx: dict[str, Any] = {
                    "id": project.id,
                    "name": project.name,
                    "project_path": project.repo_path,
                }
                # Enrich with project.json data if available
                if project.repo_path:
                    project_file = Path(project.repo_path) / ".gobby" / "project.json"
                    if project_file.exists():
                        try:
                            data = json.loads(project_file.read_text())
                            fs_id = data.get("id")
                            if fs_id and fs_id != project.id:
                                logger.warning(
                                    "Project ID mismatch: session='%s', filesystem='%s' at %s. Using filesystem.",
                                    project.id,
                                    fs_id,
                                    project.repo_path,
                                )
                            data["project_path"] = project.repo_path
                            return set_project_context(data)
                        except (json.JSONDecodeError, OSError) as e:
                            logger.debug("Failed to read project.json at %s: %s", project_file, e)
                return set_project_context(ctx)
        except (ImportError, OSError, sqlite3.Error) as e:
            logger.debug("Failed to enrich project context for session %s: %s", session_id, e)

        return set_project_context({"id": session.project_id})

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Call a tool.

        Returns the tool result, or a CallToolResult with isError=True if the
        underlying service indicates an error. This ensures the MCP protocol
        properly signals errors to LLM clients instead of returning error dicts
        as successful responses.

        When session_id is provided and a workflow is active, checks that the
        tool is not blocked by the current workflow step's blocked_tools setting.
        """
        # Set session's project context for this call
        token = None
        call_context = None
        if session_id:
            token = self._resolve_and_set_project_context(session_id)
            # Build call_context so internal tools (e.g. canvas) can access
            # conversation_id without the caller having to pass it explicitly.
            if self._session_manager:
                session = self._session_manager.get(session_id)
                if session:
                    call_context = {
                        "session_id": session_id,
                        "conversation_id": session.external_id,
                    }
        try:
            result = await self.tool_proxy.call_tool(
                server_name, tool_name, arguments, session_id, call_context=call_context
            )
        finally:
            if token is not None:
                from gobby.utils.project_context import reset_project_context

                reset_project_context(token)

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

    # --- Fallback Resolver ---

    async def get_tool_alternatives(
        self,
        server_name: str,
        tool_name: str,
        error_message: str | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Get alternative tool suggestions when a tool fails.

        Uses semantic similarity and historical success rates to suggest
        similar tools that might work as alternatives.

        Args:
            server_name: Server where the tool failed
            tool_name: Name of the failed tool
            error_message: Optional error message for context-aware matching
            top_k: Maximum number of alternatives to return (default: 5)

        Returns:
            Dict with alternative tool suggestions and scores
        """
        project_id = self._mcp_manager.project_id
        if not project_id:
            ctx = get_project_context()
            project_id = ctx.get("id") if ctx else None
        if not project_id:
            return {
                "success": False,
                "error": "No project_id available. Run 'gobby init' first.",
            }

        fallback_resolver = self.tool_proxy._fallback_resolver
        if not fallback_resolver:
            return {
                "success": False,
                "error": "Fallback resolver not configured",
            }

        try:
            suggestions = await fallback_resolver.find_alternatives_for_error(
                server_name=server_name,
                tool_name=tool_name,
                error_message=error_message or "Tool call failed",
                project_id=project_id,
                top_k=top_k,
            )

            return {
                "success": True,
                "failed_tool": f"{server_name}/{tool_name}",
                "alternatives": suggestions,
                "count": len(suggestions),
            }
        except Exception as e:
            logger.error(f"Failed to get tool alternatives: {e}")
            return {"success": False, "error": str(e)}

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
            ctx = get_project_context()
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
        value: str | int | float | bool | None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Set a session variable. Used by enforcement gates and agent coordination."""
        if not self._session_manager or not self._session_manager.db:
            return {"ok": False, "error": "Session manager not available"}

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
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Get session variable(s). Returns one if name given, all if omitted."""
        if not self._session_manager or not self._session_manager.db:
            return {"ok": False, "error": "Session manager not available"}

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

    # Fallback Resolver
    mcp.add_tool(tools_handler.get_tool_alternatives)

    # Semantic Search
    mcp.add_tool(tools_handler.search_tools)

    # Session Variables
    mcp.add_tool(tools_handler.set_variable)
    mcp.add_tool(tools_handler.get_variable)

    # Hook Extension tools are now in gobby-plugins internal registry

    return mcp
