"""Server management service."""

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.client_manager.server_registry import config_from_server
from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPError, MCPServerConfig
from gobby.mcp_proxy.templates import MCPServerTemplate, expand_template
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore

if TYPE_CHECKING:
    from gobby.config.app import DaemonConfig
    from gobby.llm.service import LLMService

logger = logging.getLogger("gobby.mcp.server")


def _scope_project(scope: str | None, project_id: str | None) -> str | None:
    if scope == "global":
        return GLOBAL_PROJECT_ID
    return project_id


def _scope_label(project_id: str | None) -> str:
    return "global" if project_id == GLOBAL_PROJECT_ID else "project"


def _duplicate_envelope(existing: Any, name: str, scope_project: str) -> dict[str, Any]:
    template = getattr(existing, "template", None)
    return {
        "success": False,
        "error": "duplicate",
        "name": name,
        "id": getattr(existing, "id", None),
        "scope": _scope_label(str(getattr(existing, "project_id", scope_project))),
        "template": template,
    }


def _configure_commands(missing_secrets: list[str], *, global_scope: bool) -> list[str]:
    flag = " --global" if global_scope else ""
    return [f"gobby secrets set {secret}{flag}" for secret in missing_secrets]


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
        transport: str | None = None,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
        project_id: str | None = None,
        template: str | None = None,
        values: Mapping[str, str] | None = None,
        scope: str = "project",
        description: str | None = None,
    ) -> dict[str, Any]:
        """Add a new MCP server, optionally expanded from a template."""
        try:
            if not project_id:
                from gobby.utils.project_context import get_project_context

                ctx = get_project_context()
                if ctx and ctx.get("id"):
                    project_id = ctx["id"]

            scope_project = _scope_project(scope, project_id)
            if not scope_project:
                return {
                    "success": False,
                    "error": "project_id is required. Run 'gobby init' or provide project_id.",
                }

            db = getattr(self._mcp_manager, "mcp_db_manager", None)
            missing_secrets: list[str] = []
            template_name: str | None = None
            server_config: MCPServerConfig
            if template:
                if db is None:
                    return {"success": False, "error": "MCP database unavailable for templates"}
                row = db.get_template(template, project_id=scope_project)
                if row is None:
                    return {"success": False, "error": f"template '{template}' not found"}
                if not row.enabled:
                    return {
                        "success": False,
                        "error": "template_disabled",
                        "template": row.name,
                        "scope": _scope_label(str(row.project_id)),
                    }
                definition = dict(row.definition)
                definition.setdefault("name", row.name)
                tmpl = MCPServerTemplate.from_definition(definition)
                template_name = tmpl.name
                secret_store = SecretStore(db.db) if getattr(db, "db", None) is not None else None

                def secret_exists(secret_name: str) -> bool:
                    if secret_store is None:
                        return False
                    return bool(secret_store.exists(secret_name, project_id=scope_project))

                expanded = expand_template(
                    tmpl,
                    name=name,
                    project_id=scope_project,
                    values=dict(values or {}),
                    description=description,
                    secret_exists=secret_exists,
                )
                server_config = expanded.config
                server_config.template_id = row.id
                server_config.enabled = enabled
                missing_secrets = list(expanded.missing_secrets)
            else:
                if not transport:
                    return {"success": False, "error": "transport is required without a template"}
                server_config = MCPServerConfig(
                    name=name,
                    project_id=scope_project,
                    transport=transport,
                    url=url,
                    command=command,
                    args=args,
                    env=env,
                    headers=headers,
                    enabled=enabled,
                    description=description,
                )
            try:
                server_config.validate()
            except ValueError as e:
                return {"success": False, "error": f"Validation error: {e}"}

            try:
                if missing_secrets and db is not None:
                    inserted = db.insert_server(
                        name=server_config.name,
                        transport=server_config.transport,
                        project_id=server_config.project_id,
                        url=server_config.url,
                        command=server_config.command,
                        args=server_config.args,
                        env=server_config.env,
                        headers=server_config.headers,
                        enabled=server_config.enabled,
                        description=server_config.description,
                        requires_oauth=server_config.requires_oauth,
                        oauth_provider=server_config.oauth_provider,
                        connect_timeout=server_config.connect_timeout,
                        template_id=server_config.template_id,
                        template_values=server_config.template_values,
                        runtime_hook=server_config.runtime_hook,
                    )
                    if inserted is None:
                        existing = db.get_server(server_config.name, scope_project)
                        return _duplicate_envelope(existing, name, scope_project)
                    adopted = config_from_server(inserted)
                    self._mcp_manager.add_server_config(adopted)
                    add_result = {
                        "success": True,
                        "connected": False,
                        "id": adopted.id,
                        "name": adopted.name,
                    }
                    server_config = adopted
                else:
                    add_result = await self._mcp_manager.add_server(server_config)
                    if not isinstance(add_result, Mapping):
                        add_result = {}
                    if add_result.get("id"):
                        loaded = self._mcp_manager.get_server_config(str(add_result["id"]))
                        if loaded is not None:
                            server_config = loaded
            except MCPError as exc:
                if "already exists" in str(exc).lower() and db is not None:
                    existing = db.get_server(name.lower(), scope_project)
                    return _duplicate_envelope(existing, name, scope_project)
                raise
            except ValueError:
                raise
            except Exception as e:
                if self._mcp_manager.has_server(getattr(server_config, "id", name)):
                    logger.warning("Added server %s but connection failed: %s", name, e)
                    return {
                        "success": True,
                        "message": f"Server added but connection failed: {str(e)}",
                        "connected": False,
                        "id": getattr(server_config, "id", None),
                        "name": name,
                    }
                raise

            response: dict[str, Any] = {
                "success": True,
                "message": f"Server {name} added successfully",
                "connected": bool(add_result.get("connected")),
                "name": server_config.name,
                "id": add_result.get("id") or server_config.id,
                "scope": _scope_label(scope_project),
                "template": template_name,
                "missing_secrets": missing_secrets,
                "needs_configuration": bool(missing_secrets),
                "configure": _configure_commands(
                    missing_secrets, global_scope=scope_project == GLOBAL_PROJECT_ID
                ),
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

    async def remove_server(
        self, name: str, *, scope: str = "project", project_id: str | None = None
    ) -> dict[str, Any]:
        """Remove the exact `(name, scope)` MCP server row."""
        try:
            if not project_id:
                from gobby.mcp_proxy.services.server_resolution import as_project_id

                manager_project = as_project_id(
                    getattr(self._mcp_manager, "project_id", None), default=""
                )
                project_id = manager_project or None
                if not project_id:
                    from gobby.utils.project_context import get_project_context

                    ctx = get_project_context()
                    if ctx and ctx.get("id"):
                        project_id = ctx["id"]
            scope_project = _scope_project(scope, project_id)
            db = getattr(self._mcp_manager, "mcp_db_manager", None)
            if db is not None and scope_project:
                row = db.get_server(name, scope_project)
                if row is None:
                    return {
                        "success": False,
                        "error": f"Server '{name}' not found",
                    }
                await self._mcp_manager.remove_server(row.id, project_id=scope_project)
                return {"success": True, "message": f"Server {name} removed", "id": row.id}
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
