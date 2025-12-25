"""
FastMCP server for Gobby daemon control.

Provides MCP tools and resources for controlling the Gobby daemon,
including session management, status monitoring, and proxying calls
to downstream MCP servers.

Local-first version: No platform auth, no platform sync.
"""

import logging
import os
import time
from typing import TYPE_CHECKING, Any, cast

from fastmcp import FastMCP

logger = logging.getLogger("gobby.mcp.server")

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager
    from gobby.llm.base import LLMProvider
    from gobby.llm.service import LLMService
    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.sync.tasks import TaskSyncManager


_mcp_instance: FastMCP | None = None


def get_mcp_server() -> FastMCP | None:
    """Get the current MCP server instance."""
    return _mcp_instance


def create_mcp_server(
    mcp_manager: "MCPClientManager",
    daemon_port: int,
    start_time: float,
    config: Any | None = None,
    llm_service: "LLMService | None" = None,
    codex_client: Any | None = None,
    task_manager: "LocalTaskManager | None" = None,
    task_sync_manager: "TaskSyncManager | None" = None,
) -> FastMCP:
    """
    Create FastMCP server with daemon control tools.

    Args:
        mcp_manager: MCP client manager instance
        daemon_port: Daemon HTTP port
        start_time: Daemon start timestamp
        config: Optional DaemonConfig instance for tool configuration
        llm_service: Optional LLMService for multi-provider support
        codex_client: Optional CodexAppServerClient for Codex integration

    Returns:
        Configured FastMCP server instance
    """
    global _mcp_instance
    mcp = FastMCP(name="Gobby Daemon")
    _mcp_instance = mcp

    # Extract code execution config with defaults
    code_exec_enabled = True
    code_exec_model = "claude-sonnet-4-5"
    code_exec_max_turns = 5
    code_exec_timeout = 30
    code_exec_preview = 3
    code_exec_prompt: str | None = None
    code_exec_provider: str | None = None

    if config:
        code_exec_config = config.get_code_execution_config()
        if code_exec_config:
            code_exec_enabled = code_exec_config.enabled
            code_exec_model = code_exec_config.model
            code_exec_max_turns = code_exec_config.max_turns
            code_exec_timeout = code_exec_config.default_timeout
            code_exec_preview = code_exec_config.max_dataset_preview
            code_exec_prompt = code_exec_config.prompt
            code_exec_provider = code_exec_config.provider

    # Extract recommend_tools config with defaults
    recommend_tools_enabled = True
    recommend_tools_model = "claude-sonnet-4-5"
    recommend_tools_provider: str | None = None
    recommend_tools_prompt: str | None = None

    if config:
        recommend_tools_config = config.get_recommend_tools_config()
        if recommend_tools_config:
            recommend_tools_enabled = recommend_tools_config.enabled
            recommend_tools_model = recommend_tools_config.model
            recommend_tools_provider = recommend_tools_config.provider
            recommend_tools_prompt = recommend_tools_config.prompt

    # Extract MCP client proxy config with defaults
    mcp_proxy_enabled = True
    mcp_tool_timeout = 60.0

    if config:
        mcp_proxy_config = config.get_mcp_client_proxy_config()
        if mcp_proxy_config:
            mcp_proxy_enabled = mcp_proxy_config.enabled
            mcp_tool_timeout = mcp_proxy_config.tool_timeout

    # Extract import_mcp_server config - store the full config object
    import_mcp_server_config = config

    # Extract websocket port from config
    websocket_port = 8766  # Default
    if config and hasattr(config, "websocket") and config.websocket:
        websocket_port = config.websocket.port

    # Helper to get LLM provider for a feature
    def get_llm_provider_for_feature(provider_name: str | None) -> "LLMProvider | None":
        """Get LLM provider by name from LLMService."""
        if llm_service and provider_name:
            try:
                return llm_service.get_provider(provider_name)
            except ValueError as e:
                logger.warning(f"Provider '{provider_name}' not available: {e}")
        if llm_service:
            try:
                return llm_service.get_default_provider()
            except ValueError:
                pass
        return None

    # ===== INTERNAL TOOL REGISTRIES =====
    # Internal tools are accessed via call_tool(server="gobby-*", ...) for progressive disclosure

    from gobby.mcp_proxy.tools.internal import InternalRegistryManager

    internal_manager = InternalRegistryManager()

    if task_manager and task_sync_manager:
        try:
            from gobby.mcp_proxy.tools.tasks import create_task_registry

            internal_manager.add_registry(create_task_registry(task_manager, task_sync_manager))
        except Exception as e:
            logger.error(f"Failed to create task registry: {e}")

    # ===== STATUS & MONITORING TOOLS =====

    @mcp.tool
    async def status() -> dict[str, Any]:
        """
        Get current daemon status and health information.

        Returns comprehensive daemon status including uptime, connected MCP servers,
        and resource usage. Includes both structured data and a formatted message
        for display.
        """
        from pathlib import Path

        from gobby.utils.status import format_status_message

        uptime_seconds = time.time() - start_time
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        seconds = int(uptime_seconds % 60)
        uptime = f"{hours}h {minutes}m {seconds}s"

        # Get MCP server status
        mcp_servers_status = []
        for server_name, connection in mcp_manager.connections.items():
            mcp_servers_status.append(
                {
                    "name": server_name,
                    "state": connection.state.value,
                    "connected": connection.is_connected,
                    "transport": connection.config.transport,
                }
            )

        # Get PID and file paths
        pid = os.getpid()
        pid_file = str(Path.home() / ".gobby" / "gobby.pid")
        log_files = str(Path.home() / ".gobby" / "logs")

        # Format status message (local-first: no auth status)
        formatted_message = format_status_message(
            running=True,
            pid=pid,
            pid_file=pid_file,
            log_files=log_files,
            uptime=uptime,
            http_port=daemon_port,
            websocket_port=websocket_port,
        )

        return {
            "status": "running",
            "uptime": uptime,
            "uptime_seconds": int(uptime_seconds),
            "pid": pid,
            "port": daemon_port,
            "mcp_servers": mcp_servers_status,
            "mcp_server_count": len(mcp_servers_status),
            "formatted_message": formatted_message,
        }

    @mcp.tool
    async def list_mcp_servers() -> dict[str, Any]:
        """
        List all configured MCP servers and their connection status.

        Returns:
            Dict with servers list. Each server includes:
            - project_id: None for global servers, UUID string for project-scoped
        """
        servers = []
        for server_name, connection in mcp_manager.connections.items():
            server_info = {
                "name": server_name,
                "project_id": connection.config.project_id,
                "description": connection.config.description,
                "connected": connection.is_connected,
            }
            servers.append(server_info)

        return {"servers": servers}

    # ===== MCP PROXY TOOLS =====

    if mcp_proxy_enabled:

        @mcp.tool
        async def call_tool(
            server_name: str, tool_name: str, arguments: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            """
            Execute a tool on a connected MCP server.

            This is the primary way to interact with MCP servers (Supabase, memory, etc.)
            through the Gobby daemon.

            Args:
                server_name: Name of the MCP server
                    Examples: "supabase", "gobby-memory", "context7"
                    Internal servers: "gobby-tasks", "gobby-hooks"
                tool_name: Name of the specific tool to execute
                    Example: "list_tables", "search_memory_nodes", "get-library-docs"
                arguments: Dictionary of arguments required by the tool (optional)
                    Example: {"schema": "public"} or {"query": "react hooks"}

            Example usage:
            1. List Supabase tables:
               call_tool("supabase", "list_tables", {"schemas": ["public"]})

            2. Search memory:
               call_tool("gobby-memory", "search_memory_nodes", {"query": "authentication"})

            3. Get library docs:
               call_tool("context7", "get-library-docs", {"libraryId": "/react/react"})

            4. Create a task (internal):
               call_tool("gobby-tasks", "create_task", {"title": "My task"})

            Workflow:
            1. Use list_tools(server_name) to see available tools
            2. Review tool parameters and requirements
            3. Call call_tool() with appropriate arguments

            Requires:
            - Daemon must be running
            - MCP server must be configured and enabled
            - Tool must exist on the specified server

            Returns:
                Dictionary with success status and tool execution result
            """
            # Route internal tools (gobby-tasks, gobby-hooks, etc.)
            if internal_manager.is_internal(server_name):
                registry = internal_manager.get_registry(server_name)
                if not registry:
                    available = ", ".join(r.name for r in internal_manager.get_all_registries())
                    return {
                        "success": False,
                        "server": server_name,
                        "error": f"Internal server '{server_name}' not found",
                        "available_internal_servers": available,
                    }
                try:
                    result = await registry.call(tool_name, arguments or {})
                    return {"success": True, "result": result}
                except ValueError as e:
                    return {
                        "success": False,
                        "server": server_name,
                        "tool": tool_name,
                        "error": str(e),
                        "error_type": "ValueError",
                    }
                except Exception as e:
                    logger.error(f"Failed to call internal tool {tool_name}: {e}")
                    return {
                        "success": False,
                        "server": server_name,
                        "tool": tool_name,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    }

            # Route to downstream MCP servers
            try:
                # Use MCPClientManager's call_tool which handles validation
                # Pass tool_timeout from config for MCP tool calls
                result = await mcp_manager.call_tool(
                    server_name, tool_name, arguments or {}, timeout=mcp_tool_timeout
                )
                return {"success": True, "result": result}
            except TimeoutError:
                logger.error(
                    f"Tool call timed out after {mcp_tool_timeout}s: {server_name}.{tool_name}"
                )
                return {
                    "success": False,
                    "server": server_name,
                    "tool": tool_name,
                    "error": f"Tool call timed out after {mcp_tool_timeout} seconds",
                    "error_type": "TimeoutError",
                }
            except Exception as e:
                logger.error(f"Failed to call {tool_name} on {server_name}: {e}")
                return {
                    "success": False,
                    "server": server_name,
                    "tool": tool_name,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        @mcp.tool
        async def read_mcp_resource(server_name: str, resource_uri: str) -> dict[str, Any]:
            """
            Read a resource from a downstream MCP server.

            Args:
                server_name: Name of the MCP server
                resource_uri: URI of the resource to read

            Returns:
                Resource contents

            Raises:
                ValueError: If server not found or not connected
                Exception: If resource read fails
            """
            try:
                # Use MCPClientManager's read_resource which handles validation
                resource = await mcp_manager.read_resource(server_name, resource_uri)
                return {
                    "success": True,
                    "content": [item.model_dump() for item in resource.contents],
                    "mime_type": resource.mimeType if hasattr(resource, "mimeType") else None,
                }
            except Exception as e:
                logger.error(f"Failed to read {resource_uri} from {server_name}: {e}")
                return {
                    "success": False,
                    "server": server_name,
                    "uri": resource_uri,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

    # ===== DYNAMIC SERVER MANAGEMENT TOOLS =====

    @mcp.tool
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
        Add a new MCP server to the current project.

        Supports multiple transport types: http, stdio, websocket.
        Servers are always project-scoped. If no project exists for the
        current directory, one will be created automatically.

        Args:
            name: Unique server name
            transport: Transport type ("http", "stdio", "websocket")
            url: Server URL (required for http/websocket)
            headers: Custom HTTP headers (optional for http/websocket)
            command: Command to run (required for stdio)
            args: Command arguments (optional for stdio)
            env: Environment variables (optional for stdio)
            enabled: Whether server is enabled (default: True)

        Returns:
            Result dict with success status and server info
        """
        from gobby.mcp_proxy.actions import add_mcp_server as add_server_action
        from gobby.utils.project_init import initialize_project

        # Get or create project for current directory
        project_id = mcp_manager.project_id
        if not project_id:
            init_result = initialize_project()
            project_id = init_result.project_id
            # Update manager's project_id for subsequent operations
            mcp_manager.project_id = project_id

        return cast(
            dict[str, Any],
            await add_server_action(
                mcp_manager=mcp_manager,
                name=name,
                transport=transport,
                project_id=project_id,
                url=url,
                headers=headers,
                command=command,
                args=args,
                env=env,
                enabled=enabled,
            ),
        )

    @mcp.tool
    async def remove_mcp_server(name: str) -> dict[str, Any]:
        """
        Remove an MCP server from the current project.

        Removes from local database (cascades to tools).

        Args:
            name: Server name to remove

        Returns:
            Result dict with success status
        """
        from gobby.mcp_proxy.actions import remove_mcp_server as remove_server_action

        project_id = mcp_manager.project_id
        if not project_id:
            return {
                "success": False,
                "name": name,
                "error": "No project context - initialize a project first with init_project()",
            }

        return cast(
            dict[str, Any],
            await remove_server_action(
                mcp_manager=mcp_manager,
                name=name,
                project_id=project_id,
            ),
        )

    @mcp.tool
    async def import_mcp_server(
        from_project: str | None = None,
        servers: list[str] | None = None,
        github_url: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """
        Import MCP servers from various sources.

        Three import modes:
        1. **From project**: Copy servers from another Gobby project
        2. **From GitHub**: Parse repository README to extract config
        3. **From query**: Search web to find and configure an MCP server

        If no secrets are needed, the server is added immediately.
        If secrets are needed (API keys), returns a config to fill in and pass to add_mcp_server().

        Args:
            from_project: Source project name to import servers from
            servers: Optional list of specific server names to import (imports all if None)
            github_url: GitHub repository URL to parse for MCP server config
            query: Natural language search query (e.g., "exa search mcp server")

        Returns:
            On success: {"success": True, "imported": ["server1", "server2"]}
            Needs secrets: {"status": "needs_configuration", "config": {...}, "missing": ["API_KEY"]}
                          (pass the filled config to add_mcp_server())

        Examples:
            # Import all servers from another project
            import_mcp_server(from_project="my-other-project")

            # Import specific servers
            import_mcp_server(from_project="gobby", servers=["supabase", "context7"])

            # Import from GitHub
            import_mcp_server(github_url="https://github.com/anthropics/mcp-filesystem")

            # Search and import
            import_mcp_server(query="exa search mcp server")
        """
        from gobby.mcp_proxy.importer import MCPServerImporter
        from gobby.storage.database import LocalDatabase
        from gobby.utils.project_init import initialize_project

        # Get or create project for current directory
        project_id = mcp_manager.project_id
        if not project_id:
            init_result = initialize_project()
            project_id = init_result.project_id
            mcp_manager.project_id = project_id

        # Initialize database and importer
        db = LocalDatabase()
        importer = MCPServerImporter(
            config=import_mcp_server_config,
            db=db,
            current_project_id=project_id,
            mcp_client_manager=mcp_manager,
        )

        # Determine which import mode to use
        if from_project is not None:
            # Import from another project
            return cast(dict[str, Any], await importer.import_from_project(from_project, servers))

        if github_url is not None:
            # Import from GitHub repository
            return cast(dict[str, Any], await importer.import_from_github(github_url))

        if query is not None:
            # Search and import
            return cast(dict[str, Any], await importer.import_from_query(query))

        return {
            "success": False,
            "error": "Must provide one of: from_project, github_url, or query",
        }

    # ===== MCP PROXY DISCOVERY TOOLS =====
    # These tools help discover what's available on downstream servers

    if mcp_proxy_enabled:

        @mcp.tool
        async def list_tools(server: str | None = None) -> dict[str, Any]:
            """
            List tools from DOWNSTREAM/PROXIED MCP servers (NOT gobby-daemon's own tools).

            IMPORTANT: This lists tools from downstream MCP servers like context7, supabase,
            playwright, serena. It does NOT list gobby-daemon's own tools.

            Use this to discover tools available on downstream servers.

            Args:
                server: Optional downstream server name (e.g., "context7", "supabase").
                       If not provided, returns tools from all downstream servers.
                       Internal servers: "gobby-tasks", "gobby-hooks", etc.

            Returns:
                Dict with tool listings from downstream servers:
                - If server specified: {"server": "context7", "tools": [{name, brief}, ...]}
                - If no server: {"servers": [{name, tools: [{name, brief}]}, ...]}

            Example:
                # List tools for specific downstream server
                list_tools(server="context7")
                > {"server": "context7", "tools": [
                    {"name": "get-library-docs", "brief": "Fetch documentation for a library"},
                    {"name": "resolve-library-id", "brief": "Find library ID from name"}
                  ]}

                # List internal task tools
                list_tools(server="gobby-tasks")
                > {"server": "gobby-tasks", "tools": [
                    {"name": "create_task", "brief": "Create a new task in the current project."},
                    {"name": "list_tasks", "brief": "List tasks with optional filters."}
                  ]}

                # List all tools across all servers
                list_tools()
                > {"servers": [
                    {"name": "gobby-tasks", "tools": [...]},
                    {"name": "context7", "tools": [...]},
                    {"name": "supabase", "tools": [...]}
                  ]}
            """
            try:
                if server:
                    # Handle internal servers (gobby-tasks, gobby-hooks, etc.)
                    if internal_manager.is_internal(server):
                        registry = internal_manager.get_registry(server)
                        if not registry:
                            available_internal = ", ".join(
                                r.name for r in internal_manager.get_all_registries()
                            )
                            return {
                                "success": False,
                                "error": f"Internal server '{server}' not found",
                                "available_internal_servers": available_internal,
                            }
                        return {"success": True, "tools": registry.list_tools()}

                    # Find specific downstream server
                    server_config = next(
                        (s for s in mcp_manager.server_configs if s.name == server), None
                    )

                    if not server_config:
                        # Build combined available list
                        internal_names = [r.name for r in internal_manager.get_all_registries()]
                        downstream_names = [s.name for s in mcp_manager.server_configs]
                        available = ", ".join(internal_names + downstream_names)
                        return {
                            "success": False,
                            "error": f"Server '{server}' not found",
                            "available_servers": available,
                        }

                    # Return tools for this downstream server
                    tools_list = []
                    if server_config.tools:
                        for tool in server_config.tools:
                            tools_list.append(
                                {
                                    "name": tool.get("name"),
                                    "brief": tool.get("brief", "No description available"),
                                }
                            )

                    return {"success": True, "tools": tools_list}
                else:
                    # Return all servers with their tools (internal + downstream)
                    servers_list = []

                    # Add internal servers first
                    for registry in internal_manager.get_all_registries():
                        servers_list.append(
                            {"name": registry.name, "tools": registry.list_tools()}
                        )

                    # Add downstream servers
                    for server_config in mcp_manager.server_configs:
                        tools_list = []
                        if server_config.tools:
                            for tool in server_config.tools:
                                tools_list.append(
                                    {
                                        "name": tool.get("name"),
                                        "brief": tool.get("brief", "No description available"),
                                    }
                                )

                        servers_list.append({"name": server_config.name, "tools": tools_list})

                    return {"success": True, "servers": servers_list}

            except Exception as e:
                logger.error(f"Failed to list tools: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        @mcp.tool
        async def get_tool_schema(server_name: str, tool_name: str) -> dict[str, Any]:
            """
            Get full schema (inputSchema) for a specific MCP tool.

            Reads the complete tool definition including the detailed inputSchema
            from the database. This provides fast, offline access to tool schemas
            without querying the live MCP server.

            Use list_tools() first to discover available tools, then use this to get
            full details before calling the tool.

            Args:
                server_name: Name of the MCP server (e.g., "context7", "supabase")
                    Internal servers: "gobby-tasks", "gobby-hooks"
                tool_name: Name of the tool (e.g., "get-library-docs", "list_tables")

            Returns:
                Dict with tool name, description, and full inputSchema:
                {
                    "success": True,
                    "server": "context7",
                    "tool": {
                        "name": "get-library-docs",
                        "description": "Fetches comprehensive documentation...",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "libraryId": {"type": "string", ...}
                            },
                            "required": ["libraryId"]
                        }
                    }
                }

            Example:
                # First discover tools
                list_tools(server="context7")

                # Then get full schema
                get_tool_schema(server_name="context7", tool_name="get-library-docs")

                # Or for internal tools
                get_tool_schema(server_name="gobby-tasks", tool_name="create_task")
            """
            try:
                # Handle internal servers (gobby-tasks, gobby-hooks, etc.)
                if internal_manager.is_internal(server_name):
                    registry = internal_manager.get_registry(server_name)
                    if not registry:
                        available_internal = ", ".join(
                            r.name for r in internal_manager.get_all_registries()
                        )
                        return {
                            "success": False,
                            "error": f"Internal server '{server_name}' not found",
                            "available_internal_servers": available_internal,
                        }

                    schema = registry.get_schema(tool_name)
                    if not schema:
                        available_tools = [t["name"] for t in registry.list_tools()]
                        return {
                            "success": False,
                            "error": f"Tool '{tool_name}' not found on '{server_name}'",
                            "available_tools": available_tools,
                        }

                    return {"success": True, "tool": schema}

                # Check if downstream server exists in config
                server_config = next(
                    (s for s in mcp_manager.server_configs if s.name == server_name), None
                )

                if not server_config:
                    # Build combined available list
                    internal_names = [r.name for r in internal_manager.get_all_registries()]
                    downstream_names = [s.name for s in mcp_manager.server_configs]
                    available = ", ".join(internal_names + downstream_names)
                    return {
                        "success": False,
                        "error": f"Server '{server_name}' not found",
                        "available_servers": available,
                    }

                # Read tool schema from database
                if mcp_manager.mcp_db_manager is None:
                    return {
                        "success": False,
                        "error": "Database manager not available",
                    }

                # Get all cached tools for this server (use server's project_id for lookup)
                cached_tools = mcp_manager.mcp_db_manager.get_cached_tools(
                    server_name, project_id=server_config.project_id
                )
                tool = next((t for t in cached_tools if t.name == tool_name), None)

                if not tool:
                    # List available tools for helpful error message
                    available_tools = [t.name for t in cached_tools]
                    return {
                        "success": False,
                        "error": f"Tool '{tool_name}' not found on server '{server_name}'",
                        "available_tools": available_tools,
                    }

                # Build tool schema response
                tool_schema = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.input_schema or {},
                }

                return {"success": True, "tool": tool_schema}

            except Exception as e:
                logger.error(f"Failed to get tool schema for {server_name}/{tool_name}: {e}")
                return {
                    "success": False,
                    "server": server_name,
                    "tool_name": tool_name,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

    # ===== CODE EXECUTION TOOLS =====

    if code_exec_enabled:

        @mcp.tool
        async def execute_code(
            code: str,
            language: str = "python",
            context: str | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any]:
            """
            Execute code using Claude's code execution sandbox via Claude Agent SDK.

            Uses your Claude subscription (no API costs) to run code in a secure sandbox.
            Perfect for processing large datasets, performing calculations, or data analysis.

            Common use cases:
            - Filter/aggregate large MCP results (e.g., million-row Supabase queries)
            - Data transformations and analysis
            - Mathematical computations
            - Generate visualizations

            Args:
                code: The code to execute (Python only for now)
                language: Programming language (default: "python", only Python supported currently)
                context: Optional context/instructions for Claude about what the code should do
                timeout: Maximum execution time in seconds (default from config)

            Returns:
                Dict with execution results:
                {
                    "success": True,
                    "result": <execution output>,
                    "language": "python",
                    "execution_time": <seconds>
                }

            Example - Process large dataset:
                execute_code(
                    code="import pandas as pd; df = pd.DataFrame(data); df[df['value'] > 100].head(10).to_dict()",
                    context="Filter rows where value > 100 and return top 10 results"
                )

            Example - Data analysis:
                execute_code(
                    code="sum(x**2 for x in range(1000))",
                    context="Calculate sum of squares from 1 to 1000"
                )
            """
            try:
                # Get LLM provider for code execution
                provider = get_llm_provider_for_feature(code_exec_provider)

                # Use LLMProvider if available
                if provider:
                    return cast(
                        dict[str, Any],
                        await provider.execute_code(
                            code=code,
                            language=language,
                            context=context,
                            timeout=timeout,
                            prompt_template=code_exec_prompt,
                        ),
                    )

                # Fallback to legacy implementation
                import time

                from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

                # Validate language
                if language.lower() != "python":
                    return {
                        "success": False,
                        "error": f"Language '{language}' not supported. Only Python is currently supported.",
                    }

                # Build prompt for Claude to execute the code
                if context:
                    prompt = f"""Execute this Python code and return the result.

Context: {context}

Code:
```python
{code}
```

Execute the code and return the output."""
                else:
                    prompt = f"""Execute this Python code and return the result.

Code:
```python
{code}
```

Execute the code and return the output."""

                # Use configured timeout or parameter timeout
                actual_timeout = timeout if timeout is not None else code_exec_timeout

                # Configure Claude Agent SDK with code execution tool enabled
                options = ClaudeAgentOptions(
                    system_prompt="You are a code execution assistant. Execute the provided code and return results.",
                    max_turns=code_exec_max_turns,
                    model=code_exec_model,
                    allowed_tools=["code_execution"],  # Enable code execution tool
                    permission_mode="default",
                )

                # Track execution time
                start_time_exec = time.time()

                # Run async query with code execution enabled (with timeout)
                async def _run_query() -> str:
                    result_text = ""
                    async for message in query(prompt=prompt, options=options):
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    result_text += block.text
                    return result_text

                import asyncio

                try:
                    result_text: str = await asyncio.wait_for(_run_query(), timeout=actual_timeout)
                except TimeoutError:
                    return {
                        "success": False,
                        "error": f"Code execution timed out after {actual_timeout} seconds",
                        "error_type": "TimeoutError",
                        "timeout": actual_timeout,
                    }

                execution_time = time.time() - start_time_exec

                return {
                    "success": True,
                    "result": result_text.strip(),
                    "execution_time": round(execution_time, 2),
                }

            except Exception as e:
                logger.error(f"Code execution failed: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        @mcp.tool
        async def process_large_dataset(
            data: list[dict[str, Any]] | dict[str, Any],
            operation: str,
            parameters: dict[str, Any] | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any]:
            """
            Process large datasets using Claude's code execution for token optimization.

            Perfect for handling large MCP results (like million-row Supabase queries) by
            processing them in a sandbox before returning to Claude, saving massive token costs.

            Uses your Claude subscription - no API costs beyond what you're already paying.

            Args:
                data: Dataset to process (list of dicts or single dict)
                operation: What to do with the data, in natural language
                          Examples:
                          - "Filter rows where value > 100 and return top 10"
                          - "Group by user_id and sum the amounts"
                          - "Calculate average, min, max for the 'score' field"
                          - "Extract unique email addresses"
                parameters: Optional dict of parameters to use in processing
                           Example: {"threshold": 100, "limit": 10}
                timeout: Maximum execution time in seconds (default from config)

            Returns:
                Dict with processed results:
                {
                    "success": True,
                    "result": <processed data>,
                    "original_size": <input row count>,
                    "processed_size": <output row count>,
                    "reduction": <percentage reduction>,
                    "execution_time": <seconds>
                }

            Example - Filter large Supabase result:
                # Instead of sending 1M rows to Claude (500k tokens)
                # Process it first (returns 100 rows, ~50 tokens)
                process_large_dataset(
                    data=supabase_result,
                    operation="Filter users who logged in within last 7 days and are premium subscribers",
                    parameters={"days": 7}
                )

            Example - Aggregate sales data:
                process_large_dataset(
                    data=sales_data,
                    operation="Group by product_id and calculate total revenue and count",
                )
            """
            try:
                import json
                import time

                from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

                # Calculate original size
                if isinstance(data, list):
                    original_size = len(data)
                else:
                    original_size = 1

                # Build prompt for Claude with code execution
                params_str = (
                    f"\n\nParameters: {json.dumps(parameters, indent=2)}" if parameters else ""
                )

                # Use configured preview size
                preview_data = data[:code_exec_preview] if isinstance(data, list) else data

                prompt = f"""Process this dataset using Python code execution.

Operation: {operation}{params_str}

Dataset preview (first {code_exec_preview} items):
{json.dumps(preview_data, indent=2)}

Dataset size: {original_size} items

Write and execute Python code to perform the requested operation.
Return the processed data as a JSON-serializable result.

Requirements:
- Import any needed libraries (pandas, numpy, etc.)
- The full dataset is available in a variable called 'data'
- Return the final result as a Python object (list, dict, or primitive)
- Be efficient - we're processing {original_size} items"""

                # Configure Claude Agent SDK with code execution
                options = ClaudeAgentOptions(
                    system_prompt=f"""You are a data processing assistant with code execution capabilities.
You have access to a dataset with {original_size} items.
Execute Python code to process the data according to the user's operation.
The variable 'data' contains: {json.dumps(data[:2] if isinstance(data, list) else data)}
Use pandas, numpy, or standard Python as needed.""",
                    max_turns=code_exec_max_turns,
                    model=code_exec_model,
                    allowed_tools=["code_execution"],
                    permission_mode="default",
                )

                # Use configured timeout or parameter timeout
                actual_timeout = timeout if timeout is not None else code_exec_timeout

                # Track execution time
                start_time_exec = time.time()

                # Run async query with code execution enabled (with timeout)
                async def _run_dataset_query() -> str:
                    result_text = ""
                    async for message in query(prompt=prompt, options=options):
                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    result_text += block.text
                    return result_text

                import asyncio

                try:
                    result_text: str = await asyncio.wait_for(
                        _run_dataset_query(), timeout=actual_timeout
                    )
                except TimeoutError:
                    return {
                        "success": False,
                        "error": f"Dataset processing timed out after {actual_timeout} seconds",
                        "error_type": "TimeoutError",
                        "timeout": actual_timeout,
                        "operation": operation,
                    }

                execution_time = time.time() - start_time_exec

                # Try to parse result as JSON if possible
                try:
                    processed_data = json.loads(result_text.strip())
                except json.JSONDecodeError:
                    # If not valid JSON, return as-is
                    processed_data = result_text.strip()

                # Calculate processed size and reduction
                if isinstance(processed_data, list):
                    processed_size = len(processed_data)
                elif isinstance(processed_data, dict):
                    processed_size = len(processed_data)
                else:
                    processed_size = 1

                reduction_pct = (
                    round((1 - processed_size / original_size) * 100, 1) if original_size > 0 else 0
                )

                return {
                    "success": True,
                    "result": processed_data,
                    "original_size": original_size,
                    "processed_size": processed_size,
                    "reduction_percent": reduction_pct,
                    "execution_time": round(execution_time, 2),
                }

            except Exception as e:
                logger.error(f"Dataset processing failed: {e}")
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "operation": operation,
                }

    # ===== TOOL RECOMMENDATION =====

    if recommend_tools_enabled:

        @mcp.tool
        async def recommend_tools(
            task_description: str, agent_id: str | None = None
        ) -> dict[str, Any]:
            """
            Get intelligent tool recommendations for a given task.

            Uses Claude Sonnet 4.5 to analyze your task and recommend which MCP tools
            from your connected servers would be most helpful. Returns recommendations
            with suggested tool names, arguments, and workflow steps.

            Args:
                task_description: Description of what you're trying to accomplish
                                 (e.g., "Find React hooks documentation", "List database tables")
                agent_id: Optional agent profile ID to filter tools by assigned permissions
                         (e.g., "frontend-dev" agent only sees frontend-related tools)

            Returns:
                Dict with tool recommendations and usage suggestions:
                {
                    "success": True,
                    "task": "Find React hooks documentation",
                    "recommendation": "I recommend using context7 tools...",
                    "agent_profile": "frontend-dev" (if filtered),
                    "available_servers": ["context7", "playwright"],
                    "total_tools": 25
                }

            Example:
                recommend_tools("Find documentation for Supabase auth")
                recommend_tools("Debug frontend issue", agent_id="frontend-dev")
            """
            try:
                from claude_agent_sdk import ClaudeAgentOptions, query

                # For now: Use in-memory config (agent filtering not yet available)
                tools_by_server = {}
                for server_config in mcp_manager.server_configs:
                    if server_config.tools:
                        # Include tool name, description, and args for better recommendations
                        tools_by_server[server_config.name] = [
                            {
                                "name": tool.get("name"),
                                "description": tool.get("description"),
                                "args": tool.get("args", {}),
                            }
                            for tool in server_config.tools
                        ]

                if not tools_by_server:
                    return {
                        "success": False,
                        "error": "No MCP tools available. Please connect to MCP servers first.",
                    }

                if agent_id:
                    logger.debug(
                        f"Agent filtering requested (agent_id={agent_id}) but database "
                        "integration not yet implemented. Showing all tools."
                    )

                # Build prompt for Claude to analyze and recommend tools
                tools_summary = []
                for server_name, tools in tools_by_server.items():
                    tools_summary.append(f"\n**{server_name}**:")
                    for tool in tools:
                        args_info = ""
                        if tool.get("args") and tool["args"].get("properties"):
                            required = tool["args"].get("required", [])
                            params = []
                            for param_name, param_info in tool["args"]["properties"].items():
                                param_type = param_info.get("type", "any")
                                req_marker = "*" if param_name in required else ""
                                params.append(f"{param_name}{req_marker}: {param_type}")
                            args_info = f" ({', '.join(params)})"

                        tools_summary.append(
                            f"  - {tool['name']}{args_info}\n    {tool['description']}"
                        )

                prompt = f"""The user is trying to: {task_description}

Available MCP tools:
{"".join(tools_summary)}

Analyze the user's task and recommend which tool(s) would be most helpful.

Provide your response in this format:
1. A brief explanation of the recommended approach
2. Suggested tools in order of use, with:
   - Server and tool name
   - Why this tool is recommended
   - Suggested arguments (if applicable)

If multiple tools should be used together, explain the workflow.
If no tools are relevant, say so clearly."""

                # Get LLM provider for recommend_tools
                provider = get_llm_provider_for_feature(recommend_tools_provider)

                # Try to use LLMProvider's recommend_tools if available
                if provider and hasattr(provider, "recommend_tools"):
                    try:
                        recommendation_text = await provider.recommend_tools(
                            task_description=task_description,
                            tools_summary="".join(tools_summary),
                            system_prompt=recommend_tools_prompt,
                        )
                    except Exception as e:
                        logger.warning(
                            f"LLMProvider.recommend_tools failed, falling back to SDK: {e}"
                        )
                        provider = None  # Fall through to legacy

                if not provider or not hasattr(provider, "recommend_tools"):
                    # Fallback to Claude Agent SDK directly
                    # Configure Claude Agent SDK with configured model
                    options = ClaudeAgentOptions(
                        system_prompt=recommend_tools_prompt,
                        max_turns=1,
                        model=recommend_tools_model,
                        allowed_tools=[],
                        permission_mode="default",
                    )

                    # Get recommendation from Claude
                    recommendation_text = ""
                    async for message in query(prompt=prompt, options=options):
                        from claude_agent_sdk import AssistantMessage, TextBlock

                        if isinstance(message, AssistantMessage):
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    recommendation_text = block.text

                return {
                    "success": True,
                    "recommendation": recommendation_text.strip(),
                    "available_servers": list(tools_by_server.keys()),
                    "total_tools": sum(len(tools) for tools in tools_by_server.values()),
                }

            except Exception as e:
                logger.error(f"Failed to generate tool recommendations: {e}")
                return {
                    "success": False,
                    "task": task_description,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

    # ===== SESSION HOOK TOOLS =====
    # Allow other CLIs (Codex, Gemini, Antigravity) to trigger session hooks via MCP

    # Lazy-initialized HookManager instance
    _hook_manager: HookManager | None = None

    def get_hook_manager() -> "HookManager":
        """Get or create the HookManager instance."""
        nonlocal _hook_manager
        if _hook_manager is None:
            from gobby.hooks.hook_manager import HookManager

            _hook_manager = HookManager(
                daemon_host="localhost",
                daemon_port=daemon_port,
                llm_service=llm_service,
                config=config,
            )
            logger.debug("HookManager initialized for MCP call_hook tool")
        return _hook_manager

    # Map user-friendly hook names to internal kebab-case names
    HOOK_TYPE_ALIASES: dict[str, str] = {
        # User-friendly names
        "SessionStart": "session-start",
        "SessionEnd": "session-end",
        "PromptSubmit": "user-prompt-submit",
        "UserPromptSubmit": "user-prompt-submit",
        "Stop": "stop",
        "PreToolUse": "pre-tool-use",
        "PostToolUse": "post-tool-use",
        "PreCompact": "pre-compact",
        "SubagentStart": "subagent-start",
        "SubagentStop": "subagent-stop",
        "Notification": "notification",
        # Also accept kebab-case directly
        "session-start": "session-start",
        "session-end": "session-end",
        "user-prompt-submit": "user-prompt-submit",
        "stop": "stop",
        "pre-tool-use": "pre-tool-use",
        "post-tool-use": "post-tool-use",
        "pre-compact": "pre-compact",
        "subagent-start": "subagent-start",
        "subagent-stop": "subagent-stop",
        "notification": "notification",
    }

    @mcp.tool
    async def call_hook(
        hook_type: str,
        params: dict[str, Any] | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """
        Trigger a session hook for non-Claude-Code CLIs (Codex, Gemini, Antigravity).

        This tool allows other CLI tools to use the same session management hooks
        that Claude Code uses natively. It dispatches to the existing HookManager
        which handles session registration, title synthesis, summary generation, etc.

        Args:
            hook_type: Type of hook to trigger. Supported types:
                - "SessionStart" - Register session, restore context from parent
                - "PromptSubmit" - Synthesize/update session title
                - "Stop" - Mark session as paused
                - "SessionEnd" - Generate summary and prepare for handoff
            params: Hook-specific parameters:
                For SessionStart:
                    - session_id: CLI's session identifier (required)
                    - transcript_path: Path to conversation transcript
                    - source: How session started ("startup", "clear", "resume")
                    - cwd: Current working directory
                    - project_id: Optional project ID
                For PromptSubmit:
                    - session_id: CLI's session identifier (required)
                    - prompt: User's prompt text (required)
                    - transcript_path: Path to transcript
                For Stop/SessionEnd:
                    - session_id: CLI's session identifier (required)
                    - transcript_path: Path to transcript (required for summary)
            source: CLI source identifier (e.g., "Codex", "Gemini", "Antigravity").
                   Overrides the default "Claude Code" source for session tracking.

        Returns:
            Hook execution result. For SessionStart, includes:
            - session_id: Registered session UUID
            - machine_id: Machine identifier
            - parent_session_id: Parent session if handoff
            - restored_summary: Context from parent session

        Example:
            # Start a new session from Codex
            call_hook(
                hook_type="SessionStart",
                params={"session_id": "codex-abc123", "source": "startup"},
                source="Codex"
            )

            # Update title after user prompt
            call_hook(
                hook_type="PromptSubmit",
                params={"session_id": "codex-abc123", "prompt": "Help me refactor auth"}
            )

            # End session and generate summary
            call_hook(
                hook_type="SessionEnd",
                params={"session_id": "codex-abc123", "transcript_path": "/path/to/transcript"}
            )
        """
        import asyncio

        try:
            # Normalize hook type
            normalized_hook_type = HOOK_TYPE_ALIASES.get(hook_type)
            if not normalized_hook_type:
                available = ", ".join(sorted({k for k in HOOK_TYPE_ALIASES.keys() if "-" not in k}))
                return {
                    "success": False,
                    "error": f"Unknown hook type: {hook_type}",
                    "available_types": available,
                }

            # Build input data
            input_data = dict(params) if params else {}

            # Override source if provided (for non-Claude-Code CLIs)
            if source:
                input_data["_cli_source"] = source

            # Get HookManager and execute
            hook_manager = get_hook_manager()

            # Override source on the hook manager if specified
            original_source = None
            original_session_manager_source = None
            if source:
                original_source = hook_manager.SOURCE
                hook_manager.SOURCE = source
                # Also update SessionManager source (uses .source not ._source)
                if hasattr(hook_manager, "_session_manager"):
                    original_session_manager_source = hook_manager._session_manager.source
                    hook_manager._session_manager.source = source

            try:
                # Force daemon status to "ready" since we're inside the daemon
                # This bypasses the health check that would show "not_running" on first call
                with hook_manager._health_check_lock:
                    hook_manager._cached_daemon_is_ready = True
                    hook_manager._cached_daemon_status = "running"
                    hook_manager._cached_daemon_error = None

                # Execute hook in thread pool (HookManager uses sync code)
                result = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: hook_manager.execute(normalized_hook_type, input_data)
                )

                return {"success": True, "result": result}
            finally:
                # Restore original source
                if original_source is not None:
                    hook_manager.SOURCE = original_source
                if original_session_manager_source is not None:
                    if hasattr(hook_manager, "_session_manager"):
                        hook_manager._session_manager.source = original_session_manager_source

        except Exception as e:
            logger.error(f"Failed to execute hook {hook_type}: {e}", exc_info=True)
            return {
                "success": False,
                "hook_type": hook_type,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    # ===== CODEX INTEGRATION =====
    # Direct Codex tools via app-server protocol with automatic session tracking

    if codex_client is not None:
        # Track thread_id -> gobby session_id mapping
        _codex_session_mapping: dict[str, str] = {}
        _codex_session_lock = __import__("threading").Lock()

        @mcp.tool
        async def codex(
            prompt: str,
            thread_id: str | None = None,
            cwd: str | None = None,
            model: str | None = None,
            sandbox: str | None = None,
            approval_policy: str | None = None,
        ) -> dict[str, Any]:
            """
            Run Codex with automatic Gobby session tracking.

            This tool provides direct access to Codex via the app-server protocol,
            automatically managing Gobby sessions. New conversations start threads,
            and continued conversations resume threads.

            Args:
                prompt: The user prompt for Codex
                thread_id: Optional thread ID to continue an existing session.
                          If not provided, starts a new Codex thread.
                cwd: Working directory for the session (defaults to current directory)
                model: Model override (e.g., "gpt-5.1-codex", "o3")
                sandbox: Sandbox mode: "readOnly", "workspaceWrite", "dangerFullAccess"
                approval_policy: Approval policy: "never", "unlessTrusted", "always"

            Returns:
                Dict with Codex result and session tracking info:
                {
                    "success": True,
                    "thread_id": "...",      # Use this for follow-up calls
                    "session_id": "...",     # Gobby session ID
                    "turn_id": "...",        # Turn ID for this exchange
                    "response": "...",       # Agent's response text
                    "items": [...],          # All items from the turn
                    "usage": {...},          # Token usage stats
                    "is_continuation": False # True if this resumed a thread
                }

            Example - New session:
                codex(prompt="Help me refactor the auth module")

            Example - Continue session:
                codex(prompt="Now add unit tests", thread_id="thr_abc123")
            """
            import os

            try:
                # Ensure client is started
                if not codex_client.is_connected:
                    await codex_client.start()

                is_continuation = thread_id is not None

                if is_continuation:
                    # Resume existing thread
                    thread = await codex_client.resume_thread(thread_id)
                else:
                    # Start new thread
                    working_dir = cwd or os.getcwd()
                    thread = await codex_client.start_thread(
                        cwd=working_dir,
                        model=model,
                        approval_policy=approval_policy,
                        sandbox=sandbox,
                    )

                    # Register gobby session for new threads
                    try:
                        hook_manager = get_hook_manager()

                        # Temporarily set source to "codex"
                        original_source = hook_manager.SOURCE
                        hook_manager.SOURCE = "codex"
                        if hasattr(hook_manager, "_session_manager"):
                            hook_manager._session_manager.source = "codex"

                        try:
                            from gobby.utils.machine_id import get_machine_id

                            machine_id = get_machine_id()

                            # Register session with thread_id as external_id
                            session_id, _ = hook_manager._session_manager.register_session(
                                external_id=thread.id,
                                machine_id=machine_id,
                                cwd=working_dir,
                            )

                            # Cache the mapping
                            with _codex_session_lock:
                                _codex_session_mapping[thread.id] = session_id

                            logger.debug(
                                f"Registered Codex session: thread_id={thread.id} "
                                f"-> session_id={session_id}"
                            )

                        finally:
                            hook_manager.SOURCE = original_source
                            if hasattr(hook_manager, "_session_manager"):
                                hook_manager._session_manager.source = original_source

                    except Exception as e:
                        logger.error(f"Failed to register Codex session: {e}", exc_info=True)

                # Run the turn and collect events
                response_text = ""
                items = []
                turn_id = None
                usage = None

                async for event in codex_client.run_turn(thread.id, prompt):
                    event_type = event.get("type", "")

                    if event_type == "turn/created":
                        turn_data = event.get("turn", {})
                        turn_id = turn_data.get("id")

                    elif event_type == "item/completed":
                        item = event.get("item", {})
                        items.append(item)

                        # Extract agent message text
                        if item.get("type") == "agent_message":
                            response_text = item.get("text", "")

                    elif event_type == "turn/completed":
                        usage = event.get("usage")

                # Look up gobby session ID
                with _codex_session_lock:
                    gobby_session_id = _codex_session_mapping.get(thread.id)

                return {
                    "success": True,
                    "thread_id": thread.id,
                    "session_id": gobby_session_id,
                    "turn_id": turn_id,
                    "response": response_text,
                    "items": items,
                    "usage": usage,
                    "is_continuation": is_continuation,
                }

            except Exception as e:
                logger.error(f"Codex call failed: {e}", exc_info=True)
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        @mcp.tool
        async def codex_list_threads(limit: int = 25, cursor: str | None = None) -> dict[str, Any]:
            """
            List available Codex conversation threads.

            Args:
                limit: Maximum threads to return (default 25)
                cursor: Pagination cursor from previous call

            Returns:
                Dict with threads list and pagination info
            """
            try:
                if not codex_client.is_connected:
                    await codex_client.start()

                threads, next_cursor = await codex_client.list_threads(limit=limit, cursor=cursor)

                return {
                    "success": True,
                    "threads": [
                        {
                            "id": t.id,
                            "preview": t.preview,
                            "model_provider": t.model_provider,
                            "created_at": t.created_at,
                        }
                        for t in threads
                    ],
                    "next_cursor": next_cursor,
                }

            except Exception as e:
                logger.error(f"Failed to list Codex threads: {e}", exc_info=True)
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        @mcp.tool
        async def codex_archive_thread(thread_id: str) -> dict[str, Any]:
            """
            Archive a Codex conversation thread.

            Args:
                thread_id: ID of the thread to archive

            Returns:
                Dict with success status
            """
            try:
                if not codex_client.is_connected:
                    await codex_client.start()

                await codex_client.archive_thread(thread_id)

                # Remove from session mapping
                with _codex_session_lock:
                    _codex_session_mapping.pop(thread_id, None)

                return {"success": True}

            except Exception as e:
                logger.error(f"Failed to archive Codex thread: {e}", exc_info=True)
                return {
                    "success": False,
                    "error": str(e),
                    "error_type": type(e).__name__,
                }

        logger.debug("Codex integration tools registered")

    # ===== RESOURCES =====

    @mcp.resource("gobby://config")
    async def get_daemon_config() -> dict[str, Any]:
        """
        Get daemon configuration.

        Provides read-only access to the daemon's configuration including
        port and MCP server configurations.
        """
        return {
            "daemon_port": daemon_port,
            "mcp_servers": list(mcp_manager.connections.keys()),
        }

    logger.debug("FastMCP server created with daemon control tools")
    return mcp
