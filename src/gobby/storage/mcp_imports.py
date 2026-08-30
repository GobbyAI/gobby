"""MCP server and tool import helpers."""

import json
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from gobby.storage.mcp_models import MCPServer

logger = logging.getLogger(__name__)


@runtime_checkable
class _ImportManager(Protocol):
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
