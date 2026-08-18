"""MCP tool cache storage operations."""

import json
import logging
import uuid
from typing import Any, Protocol, cast

from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp_models import MCPServer, Tool

logger = logging.getLogger(__name__)


class _ServerLookup(Protocol):
    def get_server(self, name: str, project_id: str) -> MCPServer | None: ...


def _normalized_tool_entries(tools: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for tool in tools:
        raw_name = tool.get("name")
        tool_name = str(raw_name).strip().lower() if raw_name is not None else ""
        if not tool_name:
            continue
        if tool_name in seen:
            continue
        seen.add(tool_name)
        normalized_tool = dict(tool)
        normalized_tool["name"] = tool_name
        entries.append((tool_name, normalized_tool))
    return entries


def _tool_input_schema(tool: dict[str, Any]) -> Any:
    if "inputSchema" in tool:
        return tool["inputSchema"]
    if "input_schema" in tool:
        return tool["input_schema"]
    return tool.get("args")


class MCPToolStorageMixin:
    """Cached MCP tool CRUD methods."""

    db: HubDatabase

    def cache_tools(self, server_name: str, tools: list[dict[str, Any]], project_id: str) -> int:
        """
        Cache tools for a server.

        Replaces existing tools for the server.

        Args:
            server_name: Server name
            tools: List of tool definitions with name, description, and inputSchema (or args)
            project_id: Required project ID

        Returns:
            Number of tools cached
        """
        server = cast(_ServerLookup, self).get_server(server_name, project_id=project_id)
        if not server:
            logger.warning("Server not found: %s", server_name)
            return 0
        entries = _normalized_tool_entries(tools)

        generation_state = EmbeddingGenerationState(self.db)
        with self.db.transaction() as conn:
            stale_rows = conn.execute(
                "SELECT id FROM tools WHERE mcp_server_id = %s",
                (server.id,),
            ).fetchall()
            conn.execute("DELETE FROM tools WHERE mcp_server_id = %s", (server.id,))
            for stale_row in stale_rows:
                generation_state.append_change(
                    "tool", str(stale_row["id"]), is_tombstone=True, transaction=conn
                )
            for tool_name, tool in entries:
                tool_id = str(uuid.uuid4())
                input_schema = _tool_input_schema(tool)
                conn.execute(
                    """
                    INSERT INTO tools (id, mcp_server_id, name, description, input_schema)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        tool_id,
                        server.id,
                        tool_name,
                        tool.get("description"),
                        json.dumps(input_schema) if input_schema is not None else None,
                    ),
                )
                generation_state.append_change("tool", tool_id, transaction=conn)

        return len(entries)

    def get_cached_tools(self, server_name: str, project_id: str) -> list[Tool]:
        """
        Get cached tools for a server.

        Args:
            server_name: Server name
            project_id: Required project ID
        """
        server = cast(_ServerLookup, self).get_server(server_name, project_id=project_id)
        if not server:
            return []

        rows = self.db.fetchall(
            "SELECT * FROM tools WHERE mcp_server_id = %s ORDER BY name",
            (server.id,),
        )
        return [Tool.from_row(row) for row in rows]
