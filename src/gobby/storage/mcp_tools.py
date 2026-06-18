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
            logger.warning(f"Server not found: {server_name}")
            return 0

        # Delete existing tools
        self.db.execute("DELETE FROM tools WHERE mcp_server_id = %s", (server.id,))

        # Insert new tools
        now = datetime.now(UTC).isoformat()
        for tool in tools:
            tool_id = str(uuid.uuid4())
            # Handle both 'inputSchema' and 'args' keys (internal vs MCP standard)
            input_schema = tool.get("inputSchema") or tool.get("args")
            # Normalize tool name to lowercase
            tool_name = (tool.get("name") or "").lower()
            self.db.execute(
                """
                INSERT INTO tools (id, mcp_server_id, name, description, input_schema, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    tool_id,
                    server.id,
                    tool_name,
                    tool.get("description"),
                    json.dumps(input_schema) if input_schema else None,
                    now,
                    now,
                ),
            )

        return len(tools)

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
            logger.warning(f"Server not found: {server_name}")
            return {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "total": 0}

        stats = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
        now = datetime.now(UTC).isoformat()

        # Build map of current tools by name
        current_tool_names = set()
        for tool in tools:
            tool_name = (tool.get("name") or "").lower()
            current_tool_names.add(tool_name)

        # Get existing tools
        existing_tools = {t.name: t for t in self.get_cached_tools(server_name, project_id)}

        # Detect changes using schema hash if manager available
        if schema_hash_manager:
            changes = schema_hash_manager.check_tools_for_changes(server_name, project_id, tools)
            new_tools = set(changes["new"])
            changed_tools = set(changes["changed"])
        else:
            # Without hash manager, treat all as potentially changed
            new_tools = current_tool_names - set(existing_tools.keys())
            changed_tools = current_tool_names & set(existing_tools.keys())

        # Process each tool
        for tool in tools:
            tool_name = (tool.get("name") or "").lower()
            input_schema = tool.get("inputSchema") or tool.get("args")

            if tool_name in new_tools:
                # Add new tool
                tool_id = str(uuid.uuid4())
                self.db.execute(
                    """
                    INSERT INTO tools (id, mcp_server_id, name, description, input_schema, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        tool_id,
                        server.id,
                        tool_name,
                        tool.get("description"),
                        json.dumps(input_schema) if input_schema else None,
                        now,
                        now,
                    ),
                )
                stats["added"] += 1

                # Store hash for new tool
                if schema_hash_manager:
                    schema_hash = compute_schema_hash(input_schema)
                    schema_hash_manager.store_hash(server_name, tool_name, project_id, schema_hash)

            elif tool_name in changed_tools:
                # Update changed tool
                existing = existing_tools[tool_name]
                self.db.execute(
                    """
                    UPDATE tools
                    SET description = %s, input_schema = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        tool.get("description"),
                        json.dumps(input_schema) if input_schema else None,
                        now,
                        existing.id,
                    ),
                )
                stats["updated"] += 1

                # Update hash for changed tool
                if schema_hash_manager:
                    schema_hash = compute_schema_hash(input_schema)
                    schema_hash_manager.store_hash(server_name, tool_name, project_id, schema_hash)

            else:
                # Unchanged tool - just update verification time
                stats["unchanged"] += 1
                if schema_hash_manager:
                    schema_hash_manager.update_verification_time(server_name, tool_name, project_id)

        # Remove stale tools (tools that no longer exist on server)
        stale_tools = set(existing_tools.keys()) - current_tool_names
        for tool_name in stale_tools:
            existing = existing_tools[tool_name]
            self.db.execute("DELETE FROM tools WHERE id = %s", (existing.id,))
            stats["removed"] += 1

        # Cleanup stale hashes
        if schema_hash_manager:
            schema_hash_manager.cleanup_stale_hashes(
                server_name, project_id, list(current_tool_names)
            )

        stats["total"] = len(tools)
        logger.debug(
            f"Incremental refresh for {server_name}: "
            f"+{stats['added']} ~{stats['updated']} -{stats['removed']} ={stats['unchanged']}"
        )
        return stats
