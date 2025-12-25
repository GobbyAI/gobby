"""MCP server import functionality."""

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from gobby.config.app import DaemonConfig
from gobby.storage.database import LocalDatabase
from gobby.storage.mcp import LocalMCPManager
from gobby.storage.projects import LocalProjectManager

if TYPE_CHECKING:
    from gobby.mcp_proxy.manager import MCPClientManager

logger = logging.getLogger(__name__)

# Pattern to detect placeholder secrets like <YOUR_API_KEY>
SECRET_PLACEHOLDER_PATTERN = re.compile(r"<YOUR_[A-Z0-9_]+>")


class MCPServerImporter:
    """Handles importing MCP servers from various sources."""

    def __init__(
        self,
        config: DaemonConfig,
        db: LocalDatabase,
        current_project_id: str,
        mcp_client_manager: "MCPClientManager | None" = None,
    ):
        """
        Initialize the importer.

        Args:
            config: Daemon configuration
            db: Database connection
            current_project_id: ID of the current project to import into
            mcp_client_manager: Optional MCP client manager for live connections
        """
        self.config = config
        self.db = db
        self.current_project_id = current_project_id
        self.mcp_db_manager = LocalMCPManager(db)
        self.project_manager = LocalProjectManager(db)
        self.mcp_client_manager = mcp_client_manager
        self.import_config = config.get_import_mcp_server_config()

    async def import_from_project(
        self,
        source_project: str,
        servers: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Import MCP servers from another Gobby project.

        Args:
            source_project: Source project name or ID
            servers: Optional list of server names to import (imports all if None)

        Returns:
            Result dict with imported servers or error
        """
        # Resolve source project - try by name first, then by ID
        project = self.project_manager.get_by_name(source_project)
        if not project:
            project = self.project_manager.get(source_project)

        if not project:
            # List available projects for helpful error message
            available = self.project_manager.list()
            project_names = [p.name for p in available]
            return {
                "success": False,
                "error": f"Project '{source_project}' not found",
                "available_projects": project_names,
            }

        # Get servers from source project
        source_servers = self.mcp_db_manager.list_servers(
            project_id=project.id,
            enabled_only=False,  # Include disabled servers too
        )

        if not source_servers:
            return {
                "success": False,
                "error": f"No MCP servers found in project '{project.name}'",
            }

        # Filter by server names if specified
        if servers:
            servers_lower = [s.lower() for s in servers]
            source_servers = [s for s in source_servers if s.name.lower() in servers_lower]
            if not source_servers:
                return {
                    "success": False,
                    "error": f"None of the specified servers found in project '{project.name}'",
                    "requested": servers,
                }

        # Get existing servers in current project to skip duplicates
        existing_servers = self.mcp_db_manager.list_servers(
            project_id=self.current_project_id,
            enabled_only=False,
        )
        existing_names = {s.name.lower() for s in existing_servers}

        # Import each server
        imported = []
        skipped = []
        failed = []

        for server in source_servers:
            if server.name.lower() in existing_names:
                skipped.append(server.name)
                continue

            # Add server using action (connects and saves) or just save to db
            add_result = await self._add_server(
                name=server.name,
                transport=server.transport,
                url=server.url,
                command=server.command,
                args=server.args,
                env=server.env,
                headers=server.headers,
                enabled=server.enabled,
                description=server.description,
            )

            if add_result.get("success"):
                imported.append(server.name)
            else:
                failed.append({"name": server.name, "error": add_result.get("error")})

        result: dict[str, Any] = {
            "success": len(imported) > 0 or len(failed) == 0,
            "imported": imported,
            "message": f"Imported {len(imported)} server(s) from project '{project.name}'",
        }

        if skipped:
            result["skipped"] = skipped
            result["message"] += f" (skipped {len(skipped)} existing)"

        if failed:
            result["failed"] = failed

        return result

    async def import_from_github(self, github_url: str) -> dict[str, Any]:
        """
        Import MCP server from GitHub repository.

        Uses Claude Agent SDK to fetch and parse the README.

        Args:
            github_url: GitHub repository URL

        Returns:
            Result dict with config (may need user input for secrets)
        """
        if not self.import_config.enabled:
            return {
                "success": False,
                "error": "MCP server import is disabled in configuration",
            }

        try:
            from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

            # Build prompt to fetch and extract config
            prompt = f"""Fetch the README from this GitHub repository and extract MCP server configuration:

{github_url}

If the URL doesn't point directly to a README, try to find and fetch the README.md file.

After reading the documentation, extract the MCP server configuration as a JSON object."""

            options = ClaudeAgentOptions(
                system_prompt=self.import_config.prompt,
                max_turns=3,
                model=self.import_config.model,
                allowed_tools=["WebFetch"],
                permission_mode="default",
            )

            # Run query
            result_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_text += block.text

            # Parse and add if no secrets needed
            return await self._parse_and_add_config(result_text)

        except Exception as e:
            logger.error(f"Failed to import from GitHub: {e}")
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    async def import_from_query(self, search_query: str) -> dict[str, Any]:
        """
        Import MCP server by searching for it.

        Uses Claude Agent SDK to search and extract configuration.

        Args:
            search_query: Natural language search query

        Returns:
            Result dict with config (may need user input for secrets)
        """
        if not self.import_config.enabled:
            return {
                "success": False,
                "error": "MCP server import is disabled in configuration",
            }

        try:
            from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

            # Build prompt to search and extract config
            prompt = f"""Search for MCP server: {search_query}

Find the official documentation or GitHub repository for this MCP server.
Then fetch and read the README or installation docs.

After reading the documentation, extract the MCP server configuration as a JSON object."""

            options = ClaudeAgentOptions(
                system_prompt=self.import_config.prompt,
                max_turns=5,  # More turns for search + fetch
                model=self.import_config.model,
                allowed_tools=["WebSearch", "WebFetch"],
                permission_mode="default",
            )

            # Run query
            result_text = ""
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            result_text += block.text

            # Parse and add if no secrets needed
            return await self._parse_and_add_config(result_text)

        except Exception as e:
            logger.error(f"Failed to import from query: {e}")
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    async def _add_server(
        self,
        name: str,
        transport: str,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
        description: str | None = None,
    ) -> dict[str, Any]:
        """
        Add an MCP server using the action (connects + saves) or db-only fallback.

        Args:
            name: Server name
            transport: Transport type
            url: Server URL (for http/websocket)
            command: Command (for stdio)
            args: Command args (for stdio)
            env: Environment variables
            headers: HTTP headers
            enabled: Whether server is enabled
            description: Server description

        Returns:
            Result dict with success status
        """
        try:
            if self.mcp_client_manager:
                # Use the action which connects and saves
                from gobby.mcp_proxy.actions import add_mcp_server

                result: dict[str, Any] = await add_mcp_server(
                    mcp_manager=self.mcp_client_manager,
                    name=name,
                    transport=transport,
                    project_id=self.current_project_id,
                    url=url,
                    headers=headers,
                    command=command,
                    args=args,
                    env=env,
                    enabled=enabled,
                    description=description,
                )
                return result
            else:
                # Fallback to db-only (won't be connected until restart)
                self.mcp_db_manager.upsert(
                    name=name,
                    transport=transport,
                    project_id=self.current_project_id,
                    url=url,
                    command=command,
                    args=args,
                    env=env,
                    headers=headers,
                    enabled=enabled,
                    description=description,
                )
                return {
                    "success": True,
                    "imported": [name],
                    "message": f"Successfully added MCP server '{name}' (restart daemon to connect)",
                }

        except Exception as e:
            logger.error(f"Failed to add server '{name}': {e}")
            return {
                "success": False,
                "name": name,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    async def _parse_and_add_config(self, result_text: str) -> dict[str, Any]:
        """
        Parse LLM response and add server if no secrets needed.

        Args:
            result_text: Raw text from LLM

        Returns:
            Success result if added, or needs_configuration if secrets required
        """
        # Try to extract JSON from the response
        config = self._extract_json(result_text)

        if not config:
            return {
                "success": False,
                "error": "Could not extract valid configuration from documentation",
                "raw_response": result_text[:1000],  # Include first 1000 chars for debugging
            }

        # Check for missing secrets
        missing = self._find_missing_secrets(config)
        instructions = config.pop("instructions", None)

        if missing:
            # Secrets needed - return config for user to fill in
            result: dict[str, Any] = {
                "status": "needs_configuration",
                "config": config,
                "missing": missing,
            }
            if instructions:
                result["instructions"] = instructions
            return result

        # No secrets needed - add the server directly
        name = config.get("name")
        transport = config.get("transport")

        if not name or not transport:
            return {
                "success": False,
                "error": "Extracted config missing required fields: name or transport",
                "config": config,
            }

        return await self._add_server(
            name=name,
            transport=transport,
            url=config.get("url"),
            command=config.get("command"),
            args=config.get("args"),
            env=config.get("env"),
            headers=config.get("headers"),
            enabled=config.get("enabled", True),
            description=config.get("description"),
        )

    def _extract_json(self, text: str) -> dict[str, Any] | None:
        """
        Extract JSON object from text.

        Handles JSON in code blocks or raw JSON.

        Args:
            text: Text potentially containing JSON

        Returns:
            Parsed JSON dict or None
        """
        # Try to find JSON in code blocks first
        code_block_pattern = r"```(?:json)?\s*(\{[\s\S]*?\})\s*```"
        matches = re.findall(code_block_pattern, text)

        for match in matches:
            try:
                result: dict[str, Any] = json.loads(match)
                return result
            except json.JSONDecodeError:
                continue

        # Try to find raw JSON object
        json_pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
        matches = re.findall(json_pattern, text)

        for match in matches:
            try:
                parsed: dict[str, Any] = json.loads(match)
                # Validate it looks like a server config
                if "name" in parsed or "transport" in parsed:
                    return parsed
            except json.JSONDecodeError:
                continue

        return None

    def _find_missing_secrets(self, config: dict[str, Any]) -> list[str]:
        """
        Find placeholder secrets in config.

        Args:
            config: Server configuration dict

        Returns:
            List of placeholder secret names
        """
        missing = []

        def check_value(value: Any, path: str = "") -> None:
            if isinstance(value, str):
                match = SECRET_PLACEHOLDER_PATTERN.search(value)
                if match:
                    missing.append(match.group(0))
            elif isinstance(value, dict):
                for k, v in value.items():
                    check_value(v, f"{path}.{k}" if path else k)
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    check_value(v, f"{path}[{i}]")

        check_value(config)
        return missing
