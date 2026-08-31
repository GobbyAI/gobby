"""
Gobby Daemon Tools MCP Server.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from gobby.config.app import DaemonConfig
from gobby.config.features import ToolResultOffloadConfig
from gobby.hooks.tool_outcomes import ToolOutcomeStatus, classify_raw_tool_result
from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
    canonicalize_call_tool_wrapper,
)
from gobby.mcp_proxy.instructions import build_gobby_instructions
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.services.recommendation import RecommendationService, SearchMode
from gobby.mcp_proxy.services.result_offload import ToolResultOffloader
from gobby.mcp_proxy.services.server_mgmt import ServerManagementService
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.wait_tools import call_with_wait_heartbeat, prepare_client_guard
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tool_results import ToolResultStore
from gobby.utils import project_context as project_context_utils
from gobby.utils.session_context import (
    reset_seeded_contexts,
    resolve_and_seed_contexts,
)
from gobby.utils.version import get_version

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
        db: HubDatabase | None,
        startup_config: DaemonConfig | None = None,
        config_resolver: Callable[[], DaemonConfig | None] | None = None,
        operation_context_factory: Callable[[], AbstractContextManager[None]] | None = None,
        llm_service: Any | None = None,
        llm_service_resolver: Callable[[], Any | None] | None = None,
        session_manager: Any | None = None,
        memory_manager: Any | None = None,
        config_manager: Any | None = None,
        semantic_search: Any | None = None,
        fallback_resolver: Any | None = None,
        hook_manager_resolver: Callable[[], Any | None] | None = None,
    ):
        self._startup_config = startup_config
        self._config_resolver = config_resolver
        self.internal_manager = internal_manager
        self._mcp_manager = mcp_manager  # Store for project_id access
        self._semantic_search = semantic_search  # Store for direct search access
        self._session_manager = session_manager  # Store for per-call project resolution
        self.daemon_port = daemon_port
        self.websocket_port = websocket_port
        self.start_time = start_time

        def resolve_llm_service() -> Any | None:
            if llm_service_resolver is not None:
                resolved = llm_service_resolver()
                return llm_service if resolved is None else resolved
            return llm_service

        def resolve_recommend_config() -> Any | None:
            config = self.resolve_config()
            return config.recommend_tools if config is not None else None

        # Initialize services
        result_offloader = None
        if db is not None:

            def resolve_offload_config() -> ToolResultOffloadConfig:
                config = self.config
                configured = config.get_tool_result_offload_config() if config else None
                return (
                    configured
                    if isinstance(configured, ToolResultOffloadConfig)
                    else ToolResultOffloadConfig()
                )

            result_store = ToolResultStore(db, resolve_offload_config)
            result_offloader = ToolResultOffloader(
                result_store,
                db,
                resolve_offload_config,
                self._caller_project_ref,
            )
        self.tool_proxy = ToolProxyService(
            mcp_manager,
            internal_manager=internal_manager,
            fallback_resolver=fallback_resolver,
            hook_manager_resolver=hook_manager_resolver,
            result_offloader=result_offloader,
            operation_context_factory=operation_context_factory,
        )
        self.server_mgmt = ServerManagementService(
            mcp_manager,
            config_manager,
            self.resolve_config,
            llm_service=llm_service,
            llm_service_resolver=resolve_llm_service,
        )
        self.recommendation = RecommendationService(
            llm_service,
            mcp_manager,
            db=db,
            semantic_search=semantic_search,
            project_id=None,  # Resolved per-call via get_project_context()
            config_resolver=resolve_recommend_config,
            llm_service_resolver=resolve_llm_service,
        )

    def resolve_config(self) -> DaemonConfig | None:
        """Resolve current configuration with the pre-start fallback."""
        config = self._config_resolver() if self._config_resolver is not None else None
        return config if config is not None else self._startup_config

    @property
    def config(self) -> DaemonConfig | None:
        return self.resolve_config()

    def _caller_project_ref(self) -> str | None:
        ctx = project_context_utils.get_project_context()
        project_id = ctx.get("id") if ctx else None
        if project_id:
            return str(project_id)
        manager_project_id = getattr(self._mcp_manager, "project_id", None)
        if isinstance(manager_project_id, str) and manager_project_id:
            return manager_project_id
        return None

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

    async def list_mcp_servers(
        self,
        name_filter: str | None = None,
        session_id: str | None = None,
        scope: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """List configured MCP servers visible to the caller."""
        from gobby.mcp_proxy.services.server_resolution import caller_project_id

        scope_project = caller_project_id(
            self.tool_proxy,
            project_id=project_id or self._caller_project_ref(),
            scope=scope,
        )
        result = await self.tool_proxy.list_servers(name_filter, project_id=scope_project)
        db = getattr(self._mcp_manager, "mcp_db_manager", None)
        if db is not None and hasattr(db, "list_templates"):
            templates = []
            for row in db.list_templates(project_id=scope_project, enabled_only=False):
                definition = dict(row.definition or {})
                params = []
                for raw in definition.get("params") or []:
                    if not isinstance(raw, dict):
                        continue
                    params.append(
                        {
                            "name": raw.get("name"),
                            "required": bool(raw.get("required")),
                            "secret": bool(raw.get("secret")),
                            "choices": raw.get("choices") or [],
                            "default": raw.get("default"),
                        }
                    )
                templates.append(
                    {
                        "name": row.name,
                        "description": definition.get("description", ""),
                        "params": params,
                    }
                )
            result["templates"] = templates
        await asyncio.to_thread(self.tool_proxy.record_servers_listed, session_id)
        return result

    # --- Tool Proxying ---

    async def call_tool(
        self,
        server_name: str | None = None,
        tool_name: str | None = None,
        arguments: str | dict[str, Any] | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
        intent: str | None = None,
    ) -> Any:
        """Execute a tool on a connected MCP server — the primary way to reach
        Gobby's sub-servers (tasks, memory, skills, ...).

        Pass `arguments` as a dict; `session_id` is the caller's session ref
        (#N or UUID) and `project_id` targets another project. Full call-context
        semantics live in the server instructions. Errors return a
        CallToolResult with is_error=True so MCP clients see real failures.
        """
        try:
            canonical = canonicalize_call_tool_wrapper(
                server_name=server_name,
                tool_name=tool_name,
                arguments=arguments,
                session_id=session_id,
                project_id=project_id,
                intent=intent,
            )
        except CallToolWrapperInputError as exc:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error: {exc}")],
                is_error=True,
            )

        server_name = canonical.server_name
        tool_name = canonical.tool_name
        arguments = canonical.arguments
        session_id = canonical.session_id
        project_id = canonical.project_id
        intent = canonical.intent

        if not server_name or not tool_name:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Error: 'server_name' and 'tool_name' are required.",
                    )
                ],
                is_error=True,
            )

        # Infrastructure precondition: explicit project_id requires a DB.
        # Distinct from "project not found" — conflating the two makes
        # diagnosis harder.
        if project_id and (self._session_manager is None or self._session_manager.db is None):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text="Error: project_id provided but no database available to resolve it.",
                    )
                ],
                is_error=True,
            )

        db = self._session_manager.db if self._session_manager else None
        session_scope_ref = (
            self._caller_project_ref() if session_id and session_id.lstrip("#").isdigit() else None
        )
        tokens = await resolve_and_seed_contexts(
            session_ref=session_id,
            session_manager=self._session_manager,
            project_ref=project_id,
            session_scope_ref=session_scope_ref,
            session_ref_origin="ambient",
            db=db,
        )

        # User-input error: caller passed project_id but it did not resolve.
        if project_id and tokens.resolved_project_id is None:
            reset_seeded_contexts(tokens)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Error: project_id '{project_id}' not found. "
                        "Use a valid project UUID or name.",
                    )
                ],
                is_error=True,
            )
        if session_id and tokens.resolved_session_id is None:
            reset_seeded_contexts(tokens)
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Error: session_id '{session_id}' not found. "
                        "Use a valid session UUID or local #N reference.",
                    )
                ],
                is_error=True,
            )
        # Propagate only the resolved platform UUID. Falling back to the raw
        # ref would re-poison workflow checks and tool filters.
        effective_session_id = tokens.resolved_session_id

        try:
            guard = prepare_client_guard(tool_name=tool_name, arguments=arguments)
            result = await call_with_wait_heartbeat(
                self.tool_proxy.call_tool(
                    server_name,
                    tool_name,
                    guard.arguments,
                    effective_session_id,
                    wrapper_originated=True,
                    intent=intent,
                    project_id=project_id or self._caller_project_ref(),
                ),
                ctx=None,
                tool_name=tool_name,
                timeout=guard.timeout,
            )
        finally:
            reset_seeded_contexts(tokens)

        if isinstance(result, dict) and guard.wait_timeout_capped:
            metadata = result.get("_mcp_metadata")
            metadata = dict(metadata) if isinstance(metadata, dict) else {}
            metadata.update(
                {
                    "requested_timeout_seconds": guard.requested_timeout_seconds,
                    "effective_timeout_seconds": guard.effective_timeout_seconds,
                    "wait_timeout_capped_by_mcp_wrapper": True,
                }
            )
            result["_mcp_metadata"] = metadata

        outcome = classify_raw_tool_result(result)
        if outcome.status is ToolOutcomeStatus.FAILED:
            if isinstance(result, CallToolResult):
                return result
            if isinstance(result, dict):
                error_msg = result.get("error", "Unknown error")
                hint = result.get("hint", "")
                schema = result.get("schema")
            else:
                error_msg = "Tool returned no result"
                hint = ""
                schema = None

            parts = [f"Error: {error_msg}"]
            if hint:
                parts.append(f"\n{hint}")
            if schema:
                parts.append(f"\nCorrect schema:\n{json.dumps(schema, indent=2)}")

            return CallToolResult(
                content=[TextContent(type="text", text="\n".join(parts))],
                is_error=True,
            )

        if isinstance(result, dict):
            # Strip redundant success field from successful responses
            if "success" in result:
                result = {k: v for k, v in result.items() if k != "success"}

        return result

    async def list_tools(self, server_name: str, session_id: str | None = None) -> dict[str, Any]:
        """List tools for a specific server, optionally filtered by workflow phase restrictions."""
        return await self.tool_proxy.list_tools(
            server_name,
            session_id=session_id,
            project_id=self._caller_project_ref(),
        )

    async def get_tool_schema(
        self,
        server_name: str,
        tool_name: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Get tool schema."""
        return await self.tool_proxy.get_tool_schema(
            server_name, tool_name, project_id=self._caller_project_ref()
        )

    async def read_mcp_resource(self, server_name: str, resource_uri: str) -> Any:
        """Read resource."""
        return await self.tool_proxy.read_resource(
            server_name, resource_uri, project_id=self._caller_project_ref()
        )

    # --- Server Management ---

    async def add_mcp_server(
        self,
        name: str,
        transport: str | None = None,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        enabled: bool = True,
        template: str | None = None,
        values: dict[str, str] | None = None,
        scope: str = "project",
        description: str | None = None,
    ) -> dict[str, Any]:
        """Add server, optionally from a template."""
        return await self.server_mgmt.add_server(
            name,
            transport,
            url,
            command,
            args,
            env,
            headers,
            enabled,
            project_id=self._caller_project_ref(),
            template=template,
            values=values,
            scope=scope,
            description=description,
        )

    async def remove_mcp_server(self, name: str, scope: str = "project") -> dict[str, Any]:
        """Remove server in the requested scope."""
        return await self.server_mgmt.remove_server(
            name, scope=scope, project_id=self._caller_project_ref()
        )

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

        project_id = self._caller_project_ref()
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
            logger.error("Semantic search failed: %s", e)
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
        scope: Literal["session", "step"] = "session",
    ) -> dict[str, Any]:
        """Set a variable. Session-scoped by default. Pass scope='step' for the session instance."""
        if not self._session_manager or not self._session_manager.db:
            return {"success": False, "error": "Session manager not available"}

        from gobby.mcp_proxy.tools.workflows._variables import set_variable as _set_var
        from gobby.workflows.step_instances import AgentStepInstanceManager

        instance_manager = (
            AgentStepInstanceManager(self._session_manager.db) if scope == "step" else None
        )
        return await asyncio.to_thread(
            _set_var,
            self._session_manager,
            self._session_manager.db,
            name,
            value,
            session_id,
            scope=scope,
            instance_manager=instance_manager,
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
        scope: Literal["session", "step"] = "session",
    ) -> dict[str, Any]:
        """Get a variable (or all variables). Session-scoped by default. Pass scope='step' for the session instance."""
        if not self._session_manager or not self._session_manager.db:
            return {"success": False, "error": "Session manager not available"}

        from gobby.mcp_proxy.tools.workflows._variables import get_variable as _get_var
        from gobby.workflows.step_instances import AgentStepInstanceManager

        instance_manager = (
            AgentStepInstanceManager(self._session_manager.db) if scope == "step" else None
        )
        return await asyncio.to_thread(
            _get_var,
            self._session_manager,
            self._session_manager.db,
            name,
            session_id,
            scope=scope,
            instance_manager=instance_manager,
        )

    # Hook Extension tools migrated to gobby-plugins internal registry
    # (see src/gobby/mcp_proxy/tools/plugins/)


def create_mcp_server(tools_handler: GobbyDaemonTools) -> MCPServer:
    """Create the MCPServer instance for the HTTP daemon."""
    mcp = MCPServer("gobby", instructions=build_gobby_instructions(), version=get_version())

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
