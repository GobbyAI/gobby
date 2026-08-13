"""Server management service."""

import logging
from collections.abc import Callable
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
        config_resolver: "Callable[[], DaemonConfig | None] | None" = None,
        llm_service: "LLMService | None" = None,
        llm_service_resolver: "Callable[[], LLMService | None] | None" = None,
    ):
        """
        Args:
            mcp_manager: MCP client manager
            config_manager: Config manager (for saving changes)
            config_resolver: Current Daemon configuration resolver
            llm_service: LLM service for SDK calls in import operations
        """
        self._mcp_manager = mcp_manager
        self._config_manager = config_manager
        self._config_resolver = config_resolver
        self._llm_service = llm_service
        self._llm_service_resolver = llm_service_resolver

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

            try:
                add_result = await self._mcp_manager.add_server(server_config)
            except ValueError:
                raise
            except Exception as e:
                if self._mcp_manager.has_server(name):
                    logger.warning("Added server %s but connection failed: %s", name, e)
                    return {
                        "success": True,
                        "message": f"Server added but connection failed: {str(e)}",
                        "connected": False,
                    }
                raise

            response: dict[str, Any] = {
                "success": True,
                "message": f"Server {name} added successfully",
                "connected": bool(add_result.get("connected")),
            }
            if add_result.get("error") is not None:
                response["error"] = add_result["error"]
                response["message"] = f"Server {name} added but connection failed"
            return response

        except ValueError:
            raise
        except Exception as e:
            logger.exception("Unexpected error adding server %s", name)
            return {"success": False, "error": str(e)}

    async def remove_server(self, name: str) -> dict[str, Any]:
        """Remove an MCP server.

        Disconnects the server first if connected, then removes the configuration.
        """
        try:
            await self._mcp_manager.remove_server(name)
            return {"success": True, "message": f"Server {name} removed"}
        except Exception as e:
            logger.error("Failed to remove server %s: %s", name, e)
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

        config = self._config_resolver() if self._config_resolver is not None else None
        if config is None:
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
            resolved_llm = (
                self._llm_service_resolver()
                if self._llm_service_resolver is not None
                else self._llm_service
            )
            if resolved_llm is None:
                resolved_llm = self._llm_service
            importer = MCPServerImporter(
                config=config,
                db=db,
                current_project_id=current_project_id,
                mcp_client_manager=self._mcp_manager,
                llm_service=resolved_llm,
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
