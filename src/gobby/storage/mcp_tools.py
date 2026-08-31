"""MCP tool cache storage operations."""

import json
import logging
import uuid
from typing import Any

from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp_models import Tool

logger = logging.getLogger(__name__)


def _normalized_tool_entries(tools: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    entries: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for tool in tools:
        raw_name = tool.get("name")
        tool_name = str(raw_name).strip() if raw_name is not None else ""
        if not tool_name:
            continue
        # Dedup case-insensitively, but cache the server's exact casing: the
        # cached name is the executable identifier handed back by discovery
        # and semantic search, and live servers match it case-sensitively.
        folded = tool_name.lower()
        if folded in seen:
            continue
        seen.add(folded)
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

    def cache_tools(self, server_id: str, tools: list[dict[str, Any]]) -> int:
        """Replace cached tools for a server, keyed by `tools.mcp_server_id`."""
        exists = self.db.fetchone("SELECT id FROM mcp_servers WHERE id = %s", (server_id,))
        if not exists:
            logger.warning("Server not found: %s", server_id)
            return 0
        entries = _normalized_tool_entries(tools)

        generation_state = EmbeddingGenerationState(self.db)
        with self.db.transaction() as conn:
            stale_rows = conn.execute(
                "SELECT id FROM tools WHERE mcp_server_id = %s",
                (server_id,),
            ).fetchall()
            conn.execute("DELETE FROM tools WHERE mcp_server_id = %s", (server_id,))
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
                        server_id,
                        tool_name,
                        tool.get("description"),
                        json.dumps(input_schema) if input_schema is not None else None,
                    ),
                )
                generation_state.append_change("tool", tool_id, transaction=conn)

        return len(entries)

    def get_cached_tools(self, server_id: str) -> list[Tool]:
        """Return cached tools for a server id."""
        exists = self.db.fetchone("SELECT id FROM mcp_servers WHERE id = %s", (server_id,))
        if not exists:
            return []

        rows = self.db.fetchall(
            "SELECT * FROM tools WHERE mcp_server_id = %s ORDER BY name",
            (server_id,),
        )
        return [Tool.from_row(row) for row in rows]
