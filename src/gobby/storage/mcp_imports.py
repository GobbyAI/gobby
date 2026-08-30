"""MCP server and tool import helpers."""

import json
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from gobby.storage.mcp_models import MCPServer

logger = logging.getLogger(__name__)


@runtime_checkable
class _ImportManager(Protocol):
    def upsert(
        self,
        *,
        name: str,
        transport: str,
        project_id: str,
        **fields: Any,
    ) -> MCPServer: ...

    def get_server(self, name: str, project_id: str) -> MCPServer | None: ...

    def cache_tools(
        self,
        server_id: str,
        tools: list[dict[str, Any]],
    ) -> int: ...


def _import_manager(host: object) -> _ImportManager:
    if not isinstance(host, _ImportManager):
        raise TypeError("MCPImportStorageMixin requires MCP server and tool storage methods")
    return host


class MCPImportStorageMixin:
    """MCP JSON and filesystem import methods."""

    def _upsert_imported_mcp_server(
        self,
        *,
        name: str,
        config: dict[str, Any],
        project_id: str,
    ) -> None:
        manager = _import_manager(self)
        transport = config.get("transport", "stdio")
        manager.upsert(
            name=name,
            transport=transport,
            url=config.get("url"),
            command=config.get("command"),
            args=config.get("args"),
            env=config.get("env"),
            headers=config.get("headers"),
            enabled=config.get("enabled", True),
            description=config.get("description"),
            requires_oauth=config.get("requires_oauth"),
            oauth_provider=config.get("oauth_provider"),
            connect_timeout=config.get("connect_timeout"),
            project_id=project_id,
        )

    def import_from_mcp_json(self, path: str | Path, project_id: str) -> int:
        """
        Import servers from .mcp.json file.

        Supports both formats:
        - Claude Code format: {"mcpServers": {"server_name": {...}, ...}}
        - Gobby format: {"servers": [{"name": "server_name", ...}, ...]}

        Args:
            path: Path to .mcp.json file
            project_id: Required project ID

        Returns:
            Number of servers imported
        """
        path = Path(path)
        if not path.exists():
            return 0

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to read %s: %s", path, e)
            return 0
        if not isinstance(data, dict):
            return 0

        imported = 0

        # Handle Gobby format: {"servers": [{"name": "...", ...}, ...]}
        if "servers" in data and isinstance(data["servers"], list):
            for config in data["servers"]:
                if not isinstance(config, dict):
                    continue
                name = config.get("name")
                if not name:
                    continue

                self._upsert_imported_mcp_server(
                    name=str(name),
                    config=config,
                    project_id=project_id,
                )
                imported += 1

        # Handle Claude Code format: {"mcpServers": {"server_name": {...}, ...}}
        elif "mcpServers" in data and isinstance(data["mcpServers"], dict):
            for name, config in data["mcpServers"].items():
                if not isinstance(config, dict):
                    continue
                self._upsert_imported_mcp_server(
                    name=str(name),
                    config=config,
                    project_id=project_id,
                )
                imported += 1

        return imported

    def import_tools_from_filesystem(
        self, project_id: str, tools_dir: str | Path | None = None
    ) -> int:
        """
        Import tool schemas from filesystem directory.

        Reads tool JSON files from ~/.gobby/tools/<server_name>/<tool_name>.json
        and caches them in the database for servers that exist in the project.

        Args:
            project_id: Required project ID
            tools_dir: Path to tools directory (default: ~/.gobby/tools)

        Returns:
            Number of tools imported
        """
        if tools_dir is None:
            tools_dir = Path.home() / ".gobby" / "tools"
        else:
            tools_dir = Path(tools_dir)

        if not tools_dir.exists():
            return 0

        total_imported = 0
        manager = _import_manager(self)

        # Iterate through server directories
        for server_dir in tools_dir.iterdir():
            if not server_dir.is_dir() or server_dir.name.startswith("."):
                continue

            server_name = server_dir.name

            # Check if server exists in database for this project
            server = manager.get_server(server_name, project_id=project_id)
            if not server:
                logger.debug("Skipping tools for unknown server: %s", server_name)
                continue

            # Collect all tool schemas for this server
            tools: list[dict[str, Any]] = []
            for tool_file in server_dir.glob("*.json"):
                try:
                    with open(tool_file, encoding="utf-8") as f:
                        tool_data = json.load(f)
                    tools.append(
                        {
                            "name": tool_data.get("name", tool_file.stem),
                            "description": tool_data.get("description"),
                            "inputSchema": tool_data.get("inputSchema", {}),
                        }
                    )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to read tool file %s: %s", tool_file, e)
                    continue

            # Cache tools to database
            if tools:
                count = manager.cache_tools(server.id, tools)
                total_imported += count
                logger.info("Imported %s tools for server %s", count, server_name)

        return total_imported
