"""MCP tool cache storage operations."""

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol, cast

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
    return tool.get("args")


class MCPToolStorageMixin:
    """Cached MCP tool CRUD and incremental refresh methods."""

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

        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM tools WHERE mcp_server_id = %s", (server.id,))
            for tool_name, tool in entries:
                tool_id = str(uuid.uuid4())
                input_schema = _tool_input_schema(tool)
                conn.execute(
                    """
                    INSERT INTO tools (id, mcp_server_id, name, description, input_schema, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tool_id,
                        server.id,
                        tool_name,
                        tool.get("description"),
                        json.dumps(input_schema) if input_schema is not None else None,
                        now,
                        now,
                    ),
                )

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

    def refresh_tools_incremental(
        self,
        server_name: str,
        tools: list[dict[str, Any]],
        project_id: str,
        schema_hash_manager: Any | None = None,
    ) -> dict[str, Any]:
        """
        Incrementally refresh tools for a server.

        Only updates tools that have changed based on schema hash comparison.
        New tools are added, changed tools are updated, removed tools are deleted.

        Args:
            server_name: Server name
            tools: List of current tool definitions from the server
            project_id: Required project ID
            schema_hash_manager: Optional SchemaHashManager for change detection.
                If not provided, falls back to full cache_tools() behavior.

        Returns:
            Dict with refresh statistics:
            - added: number of new tools added
            - updated: number of changed tools updated
            - removed: number of stale tools removed
            - unchanged: number of unchanged tools skipped
            - total: total tools after refresh
        """
        from gobby.mcp_proxy.schema_hash import compute_schema_hash

        server = cast(_ServerLookup, self).get_server(server_name, project_id=project_id)
        if not server:
            logger.warning("Server not found: %s", server_name)
            return {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "total": 0}
        entries = _normalized_tool_entries(tools)
        normalized_tools = [tool for _tool_name, tool in entries]

        stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
        now = datetime.now(UTC).isoformat()

        current_tool_names = {tool_name for tool_name, _tool in entries}

        # Get existing tools
        existing_tools = {
            str(t.name).strip().lower(): t for t in self.get_cached_tools(server_name, project_id)
        }

        # Detect changes using schema hash if manager available
        if schema_hash_manager:
            changes = schema_hash_manager.check_tools_for_changes(
                server_name,
                project_id,
                normalized_tools,
            )
            new_tools = {str(name).strip().lower() for name in changes["new"]}
            changed_tools = {str(name).strip().lower() for name in changes["changed"]}
        else:
            # Without hash manager, treat all as potentially changed
            new_tools = current_tool_names - set(existing_tools.keys())
            changed_tools = current_tool_names & set(existing_tools.keys())

        # Process each tool
        with self.db.transaction() as conn:
            for tool_name, tool in entries:
                input_schema = _tool_input_schema(tool)

                if tool_name in new_tools:
                    tool_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO tools (id, mcp_server_id, name, description, input_schema, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            tool_id,
                            server.id,
                            tool_name,
                            tool.get("description"),
                            json.dumps(input_schema) if input_schema is not None else None,
                            now,
                            now,
                        ),
                    )
                    stats["added"] += 1

                    if schema_hash_manager:
                        schema_hash = compute_schema_hash(input_schema)
                        schema_hash_manager.store_hash(
                            server_name,
                            tool_name,
                            project_id,
                            schema_hash,
                        )

                elif tool_name in changed_tools:
                    existing = existing_tools[tool_name]
                    conn.execute(
                        """
                        UPDATE tools
                        SET description = %s, input_schema = %s, updated_at = %s
                        WHERE id = %s
                        """,
                        (
                            tool.get("description"),
                            json.dumps(input_schema) if input_schema is not None else None,
                            now,
                            existing.id,
                        ),
                    )
                    stats["updated"] += 1

                    if schema_hash_manager:
                        schema_hash = compute_schema_hash(input_schema)
                        schema_hash_manager.store_hash(
                            server_name,
                            tool_name,
                            project_id,
                            schema_hash,
                        )

                else:
                    stats["unchanged"] += 1
                    if schema_hash_manager:
                        schema_hash_manager.update_verification_time(
                            server_name,
                            tool_name,
                            project_id,
                        )

            stale_tools = set(existing_tools.keys()) - current_tool_names
            for tool_name in stale_tools:
                existing = existing_tools[tool_name]
                conn.execute("DELETE FROM tools WHERE id = %s", (existing.id,))
                stats["removed"] += 1

            if schema_hash_manager:
                schema_hash_manager.cleanup_stale_hashes(
                    server_name, project_id, list(current_tool_names)
                )

        stats["total"] = len(entries)
        logger.debug(
            "Incremental refresh for %s: +%s ~%s -%s =%s",
            server_name,
            stats["added"],
            stats["updated"],
            stats["removed"],
            stats["unchanged"],
        )
        return stats
