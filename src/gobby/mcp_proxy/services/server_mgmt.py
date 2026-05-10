"""Server management service."""

import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPServerConfig

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.llm.service import LLMService

logger = logging.getLogger("gobby.mcp.server")


class ServerManagementService:
    """Service for managing MCP server configurations."""

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        config_manager: Any,
        config: "DaemonConfig | None" = None,
        llm_service: "LLMService | None" = None,
    ):
        """
        Args:
            mcp_manager: MCP client manager
            config_manager: Config manager (for saving changes)
            config: Daemon configuration (for import operations)
            llm_service: LLM service for SDK calls in import operations
        """
        self._mcp_manager = mcp_manager
        self._config_manager = config_manager
        self._config = config
        self._llm_service = llm_service

    async def add_server(
        self,
        name: str,
        transport: str,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Add a new MCP server."""
        try:
            # Resolve project ID
            if not project_id:
                from gobby.utils.project_context import get_project_context

                ctx = get_project_context()
                if ctx and ctx.get("id"):
                    project_id = ctx["id"]

            if not project_id:
                return {
                    "success": False,
                    "error": "project_id is required. Run 'gobby init' or provide project_id.",
                }

            # Create config object
            server_config = MCPServerConfig(
                name=name,
                project_id=project_id,
                transport=transport,
                url=url,
                command=command,
                args=args,
                env=env,
                headers=headers,
                enabled=enabled,
            )
            # Validate - catch validation errors separately for clear error messages
            try:
                server_config.validate()
            except ValueError as e:
                return {"success": False, "error": f"Validation error: {e}"}

            # Add to manager (runtime)
            self._mcp_manager.add_server_config(server_config)

            # Persist to config
            # self._config_manager.add_mcp_server(...) # Mocking this interaction

            # Attempt connection
            if enabled:
                try:
                    await self._mcp_manager.connect_all([server_config])
                except Exception as e:
                    logger.warning(f"Added server {name} but connection failed: {e}")
                    return {
                        "success": True,
                        "message": f"Server added but connection failed: {str(e)}",
                        "connected": False,
                    }

            return {
                "success": True,
                "message": f"Server {name} added successfully",
                "connected": enabled,
            }

        except Exception as e:
            logger.exception(f"Unexpected error adding server {name}")
            return {"success": False, "error": str(e)}

    async def remove_server(self, name: str) -> dict[str, Any]:
        """Remove an MCP server.

        Disconnects the server first if connected, then removes the configuration.
        """
        try:
            # First disconnect if connected
            if name in self._mcp_manager._connections:
                try:
                    connection = self._mcp_manager._connections[name]
                    if connection.is_connected:
                        await connection.disconnect()
                    # Update health state
                    if name in self._mcp_manager.health:
                        from gobby.mcp_proxy.models import ConnectionState

                        self._mcp_manager.health[name].state = ConnectionState.DISCONNECTED
                    # Remove from connections
                    del self._mcp_manager._connections[name]
                    logger.info(f"Disconnected server {name} before removal")
                except Exception as e:
                    logger.warning(f"Error disconnecting server {name}: {e}")
                    # Continue with removal even if disconnect fails

            # Remove from runtime config
            self._mcp_manager.remove_server_config(name)

            # Persist
            # self._config_manager.remove_mcp_server(name)

            return {"success": True, "message": f"Server {name} removed"}
        except Exception as e:
            logger.error(f"Failed to remove server {name}: {e}")
            return {"success": False, "error": str(e)}

    async def import_server(
        self,
        from_project: str | None = None,
        github_url: str | None = None,
        query: str | None = None,
        servers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Import MCP server(s) from various sources.

        Args:
            from_project: Import from another Gobby project by name or ID
            github_url: Import from a GitHub repository URL
            query: Import by natural language search query
            servers: Optional list of specific server names to import

        Returns:
            Result dict with imported servers or error
        """
        # Validate at least one source is provided
        if not from_project and not github_url and not query:
            return {
                "success": False,
                "error": "Specify at least one: from_project, github_url, or query",
            }

        # Get current project context
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        if not project_ctx or not project_ctx.get("id"):
            return {
                "success": False,
                "error": "No current project. Run 'gobby init' first.",
            }
        current_project_id = project_ctx["id"]

        # Validate config is available
        if not self._config:
            return {
                "success": False,
                "error": "Daemon configuration not available for import operations",
            }

        try:
            # Create importer lazily with required dependencies
            from gobby.mcp_proxy.importer import MCPServerImporter

            db = getattr(self._config_manager, "db", None)
            if db is None:
                return {
                    "success": False,
                    "error": "Daemon database unavailable for import operations",
                }
            importer = MCPServerImporter(
                config=self._config,
                db=db,
                current_project_id=current_project_id,
                mcp_client_manager=self._mcp_manager,
                llm_service=self._llm_service,
            )

            # Execute import based on source
            if from_project:
                return await importer.import_from_project(
                    source_project=from_project,
                    servers=servers,
                )
            elif github_url:
                return await importer.import_from_github(github_url)
            elif query:
                return await importer.import_from_query(query)
            else:
                return {"success": False, "error": "No import source specified"}

        except Exception as e:
            logger.exception("Failed to import MCP server")
            return {"success": False, "error": str(e)}
