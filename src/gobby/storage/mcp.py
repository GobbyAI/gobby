"""Local MCP server and tool storage manager."""

import json
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.mcp_proxy.bundled import (
    BUNDLED_EXTERNAL_MCP_SERVER_NAMES,
    LEGACY_GLOBAL_PROJECT_IDS,
    canonical_project_id_for_server,
    is_bundled_external_mcp_server,
    normalize_persisted_args,
)
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import GLOBAL_PROJECT_ID

logger = logging.getLogger(__name__)


@dataclass
class MCPServer:
    """MCP server configuration model."""

    id: str
    name: str
    transport: str
    url: str | None
    command: str | None
    args: list[str] | None
    env: dict[str, str] | None
    headers: dict[str, str] | None
    enabled: bool
    description: str | None
    created_at: str
    updated_at: str
    project_id: str  # Required - all servers must belong to a project

    @classmethod
    def from_row(cls, row: Any) -> "MCPServer":
        """Create MCPServer from database row."""
        return cls(
            id=row["id"],
            name=row["name"],
            transport=row["transport"],
            url=row["url"],
            command=row["command"],
            args=json.loads(row["args"]) if row["args"] else None,
            env=json.loads(row["env"]) if row["env"] else None,
            headers=json.loads(row["headers"]) if row["headers"] else None,
            enabled=bool(row["enabled"]),
            description=row["description"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            project_id=row["project_id"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "project_id": self.project_id,
            "transport": self.transport,
            "url": self.url,
            "command": self.command,
            "args": self.args,
            "env": self.env,
            "headers": self.headers,
            "enabled": self.enabled,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_config(self) -> dict[str, Any]:
        """Convert to MCP config format."""
        config: dict[str, Any] = {
            "name": self.name,
            "transport": self.transport,
            "enabled": self.enabled,
        }
        if self.project_id:
            config["project_id"] = self.project_id
        if self.url:
            config["url"] = self.url
        if self.command:
            config["command"] = self.command
        if self.args:
            config["args"] = self.args
        if self.env:
            config["env"] = self.env
        if self.headers:
            config["headers"] = self.headers
        if self.description:
            config["description"] = self.description
        return config


@dataclass
class Tool:
    """MCP tool model."""

    id: str
    mcp_server_id: str
    name: str
    description: str | None
    input_schema: dict[str, Any] | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "Tool":
        """Create Tool from database row."""
        return cls(
            id=row["id"],
            mcp_server_id=row["mcp_server_id"],
            name=row["name"],
            description=row["description"],
            input_schema=json.loads(row["input_schema"]) if row["input_schema"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "mcp_server_id": self.mcp_server_id,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LocalMCPManager:
    """Manager for local MCP server and tool storage."""

    def __init__(self, db: DatabaseProtocol):
        """Initialize with database connection."""
        self.db = db

    def _persist_server(
        self,
        *,
        name: str,
        transport: str,
        project_id: str,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
        description: str | None = None,
    ) -> MCPServer:
        """Persist a server row without applying bundled-server cleanup."""
        server_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        self.db.execute(
            """
            INSERT INTO mcp_servers (
                id, name, project_id, transport, url, command, args, env, headers,
                enabled, description, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, project_id) DO UPDATE SET
                transport = excluded.transport,
                url = excluded.url,
                command = excluded.command,
                args = excluded.args,
                env = excluded.env,
                headers = excluded.headers,
                enabled = excluded.enabled,
                description = COALESCE(excluded.description, description),
                updated_at = excluded.updated_at
            """,
            (
                server_id,
                name,
                project_id,
                transport,
                url,
                command,
                json.dumps(args) if args else None,
                json.dumps(env) if env else None,
                json.dumps(headers) if headers else None,
                1 if enabled else 0,
                description,
                now,
                now,
            ),
        )

        server = self.get_server(name, project_id=project_id)
        if not server:
            raise RuntimeError(f"Failed to retrieve server '{name}' after upsert")
        return server

    @staticmethod
    def _server_lookup_project_ids(name: str, project_id: str) -> list[str]:
        """Return project IDs to search for a server."""
        if not is_bundled_external_mcp_server(name):
            return [project_id]

        lookup_ids = [GLOBAL_PROJECT_ID, *LEGACY_GLOBAL_PROJECT_IDS]
        if project_id not in lookup_ids:
            lookup_ids.append(project_id)
        return lookup_ids

    def _fetch_servers_by_name(self, name: str) -> list[MCPServer]:
        """Fetch every row for a server name across projects."""
        rows = self.db.fetchall(
            "SELECT * FROM mcp_servers WHERE name = ? ORDER BY updated_at DESC, created_at DESC",
            (name,),
        )
        return [MCPServer.from_row(row) for row in rows]

    def _load_tools_for_server_id(self, server_id: str) -> list[Tool]:
        """Load cached tools directly by server ID."""
        rows = self.db.fetchall(
            "SELECT * FROM tools WHERE mcp_server_id = ? ORDER BY name",
            (server_id,),
        )
        return [Tool.from_row(row) for row in rows]

    def _replace_tools_for_server_id(self, server_id: str, tools: list[Tool]) -> None:
        """Replace cached tools for a server ID."""
        self.db.execute("DELETE FROM tools WHERE mcp_server_id = ?", (server_id,))
        if not tools:
            return

        now = datetime.now(UTC).isoformat()
        for tool in tools:
            self.db.execute(
                """
                INSERT INTO tools (id, mcp_server_id, name, description, input_schema, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    server_id,
                    tool.name,
                    tool.description,
                    json.dumps(tool.input_schema) if tool.input_schema else None,
                    now,
                    now,
                ),
            )

    @staticmethod
    def _merge_tools_for_servers(tool_sets: Iterable[list[Tool]]) -> list[Tool]:
        """Union cached tool definitions by name without dropping disjoint tools."""
        merged: dict[str, Tool] = {}
        for tools in tool_sets:
            for tool in tools:
                existing = merged.get(tool.name)
                if existing is None:
                    merged[tool.name] = tool
                    continue
                merged[tool.name] = Tool(
                    id=existing.id,
                    mcp_server_id=existing.mcp_server_id,
                    name=existing.name,
                    description=existing.description or tool.description,
                    input_schema=(
                        existing.input_schema
                        if existing.input_schema is not None
                        else tool.input_schema
                    ),
                    created_at=existing.created_at,
                    updated_at=max(existing.updated_at, tool.updated_at),
                )
        return [merged[name] for name in sorted(merged)]

    @staticmethod
    def _choose_canonical_server(servers: list[MCPServer]) -> MCPServer:
        """Choose the canonical row to preserve when bundled rows are normalized."""
        for project_id in (GLOBAL_PROJECT_ID, *LEGACY_GLOBAL_PROJECT_IDS):
            for server in servers:
                if server.project_id == project_id:
                    return server
        return max(servers, key=lambda server: (server.updated_at, server.created_at, server.id))

    def upsert(
        self,
        name: str,
        transport: str,
        project_id: str,
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        enabled: bool = True,
        description: str | None = None,
    ) -> MCPServer:
        """
        Insert or update an MCP server in the database.

        Server name is normalized to lowercase.
        Uniqueness is enforced on (name, project_id) - same name can exist
        in different projects.

        Args:
            name: Server name (normalized to lowercase)
            transport: Transport type (http, stdio, websocket)
            project_id: Required project ID - all servers must belong to a project
        """
        name = name.lower()
        canonical_project_id = canonical_project_id_for_server(name, project_id)
        sanitized_args = normalize_persisted_args(name, args)

        server = self._persist_server(
            name=name,
            transport=transport,
            project_id=canonical_project_id,
            url=url,
            command=command,
            args=sanitized_args,
            env=env,
            headers=headers,
            enabled=enabled,
            description=description,
        )
        if is_bundled_external_mcp_server(name):
            self.normalize_bundled_servers([name])
            refreshed = self.get_server(name, project_id=canonical_project_id)
            if refreshed is not None:
                return refreshed
        return server

    def get_server(self, name: str, project_id: str) -> MCPServer | None:
        """
        Get server by name (case-insensitive lookup).

        Args:
            name: Server name
            project_id: Required project ID
        """
        # Normalize to lowercase for lookup
        name = name.lower()

        for lookup_project_id in self._server_lookup_project_ids(name, project_id):
            row = self.db.fetchone(
                "SELECT * FROM mcp_servers WHERE name = ? AND project_id = ?",
                (name, lookup_project_id),
            )
            if row:
                return MCPServer.from_row(row)

        return None

    def get_server_by_id(self, server_id: str) -> MCPServer | None:
        """Get server by ID."""
        row = self.db.fetchone("SELECT * FROM mcp_servers WHERE id = ?", (server_id,))
        return MCPServer.from_row(row) if row else None

    def list_servers(
        self,
        project_id: str,
        enabled_only: bool = True,
    ) -> list[MCPServer]:
        """
        List MCP servers for a project.

        Args:
            project_id: Required project ID
            enabled_only: Only return enabled servers

        Returns:
            List of servers for the project.
        """
        conditions = ["project_id = ?"]
        params: list[Any] = [project_id]

        if enabled_only:
            conditions.append("enabled = 1")

        where_clause = " AND ".join(conditions)
        query = f"SELECT * FROM mcp_servers WHERE {where_clause} ORDER BY name"  # nosec B608
        rows = self.db.fetchall(query, tuple(params))

        return [MCPServer.from_row(row) for row in rows]

    def list_all_servers(self, enabled_only: bool = True) -> list[MCPServer]:
        """
        List all MCP servers across all projects.

        Used by the daemon to load all servers on startup.

        Args:
            enabled_only: Only return enabled servers

        Returns:
            List of all servers.
        """
        if enabled_only:
            query = "SELECT * FROM mcp_servers WHERE enabled = 1 ORDER BY name"
        else:
            query = "SELECT * FROM mcp_servers ORDER BY name"
        rows = self.db.fetchall(query, ())
        servers = [MCPServer.from_row(row) for row in rows]
        bundled_servers: dict[str, list[MCPServer]] = {}
        other_servers: list[MCPServer] = []
        for server in servers:
            if is_bundled_external_mcp_server(server.name):
                bundled_servers.setdefault(server.name, []).append(server)
            else:
                other_servers.append(server)

        deduped_bundled = [
            self._choose_canonical_server(group) for _, group in sorted(bundled_servers.items())
        ]
        return sorted(deduped_bundled + other_servers, key=lambda server: server.name)

    def list_runtime_servers(self, project_id: str, enabled_only: bool = True) -> list[MCPServer]:
        """List project-scoped servers plus bundled global servers available at runtime."""
        conditions = ["enabled = 1"] if enabled_only else []
        project_where = " AND ".join(["project_id = ?"] + conditions) or "project_id = ?"
        project_rows = self.db.fetchall(
            f"SELECT * FROM mcp_servers WHERE {project_where} ORDER BY name",  # nosec B608
            (project_id,),
        )

        bundled_groups: dict[str, list[MCPServer]] = {}
        for project_scope in (GLOBAL_PROJECT_ID, *LEGACY_GLOBAL_PROJECT_IDS):
            global_conditions = ["project_id = ?"]
            params: list[Any] = [project_scope]
            if enabled_only:
                global_conditions.append("enabled = 1")
            global_conditions.append(
                "name IN ({})".format(",".join("?" for _ in BUNDLED_EXTERNAL_MCP_SERVER_NAMES))
            )
            params.extend(sorted(BUNDLED_EXTERNAL_MCP_SERVER_NAMES))
            rows = self.db.fetchall(
                f"SELECT * FROM mcp_servers WHERE {' AND '.join(global_conditions)} ORDER BY name",  # nosec B608
                tuple(params),
            )
            for row in rows:
                server = MCPServer.from_row(row)
                bundled_groups.setdefault(server.name, []).append(server)

        runtime_servers = [MCPServer.from_row(row) for row in project_rows]
        bundled_names = {
            server.name for server in runtime_servers if is_bundled_external_mcp_server(server.name)
        }
        for name, servers in sorted(bundled_groups.items()):
            if name in bundled_names:
                continue
            runtime_servers.append(self._choose_canonical_server(servers))

        return sorted(runtime_servers, key=lambda server: server.name)

    def normalize_bundled_servers(
        self,
        server_names: Iterable[str] | None = None,
    ) -> dict[str, int]:
        """Collapse bundled servers into canonical global rows."""
        names = (
            {name.lower() for name in server_names}
            if server_names is not None
            else set(BUNDLED_EXTERNAL_MCP_SERVER_NAMES)
        )
        stats = {"normalized": 0, "duplicates_removed": 0, "tools_migrated": 0}

        for name in names:
            if not is_bundled_external_mcp_server(name):
                continue

            servers = self._fetch_servers_by_name(name)
            if not servers:
                continue

            canonical_source = self._choose_canonical_server(servers)
            tools_to_preserve = self._merge_tools_for_servers(
                self._load_tools_for_server_id(server.id) for server in servers
            )

            with self.db.transaction() as conn:
                canonical_server = self._persist_server(
                    name=name,
                    transport=canonical_source.transport,
                    project_id=GLOBAL_PROJECT_ID,
                    url=canonical_source.url,
                    command=canonical_source.command,
                    args=normalize_persisted_args(name, canonical_source.args),
                    env=canonical_source.env,
                    headers=canonical_source.headers,
                    enabled=canonical_source.enabled,
                    description=canonical_source.description,
                )

                if tools_to_preserve and (
                    len(servers) > 1 or canonical_source.id != canonical_server.id
                ):
                    self._replace_tools_for_server_id(canonical_server.id, tools_to_preserve)
                    stats["tools_migrated"] += len(tools_to_preserve)

                duplicate_ids = [
                    server.id for server in servers if server.id != canonical_server.id
                ]
                for server_id in duplicate_ids:
                    conn.execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))

            if (
                canonical_source.project_id != GLOBAL_PROJECT_ID
                or normalize_persisted_args(name, canonical_source.args) != canonical_source.args
            ):
                stats["normalized"] += 1
            stats["duplicates_removed"] += len(duplicate_ids)

        return stats

    def update_server(self, name: str, project_id: str, **fields: Any) -> MCPServer | None:
        """
        Update server fields.

        Args:
            name: Server name
            project_id: Required project ID
        """
        name = name.lower()
        server = self.get_server(name, project_id=project_id)
        if not server:
            return None

        allowed = {
            "transport",
            "url",
            "command",
            "args",
            "env",
            "headers",
            "enabled",
            "description",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return server

        if "args" in fields:
            fields["args"] = normalize_persisted_args(name, fields["args"])

        if "args" in fields and fields["args"] is not None:
            fields["args"] = json.dumps(fields["args"])
        if "env" in fields and fields["env"] is not None:
            fields["env"] = json.dumps(fields["env"])
        if "headers" in fields and fields["headers"] is not None:
            fields["headers"] = json.dumps(fields["headers"])
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0

        fields["updated_at"] = datetime.now(UTC).isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        # Update by server ID to be precise
        values = list(fields.values()) + [server.id]

        self.db.execute(
            f"UPDATE mcp_servers SET {set_clause} WHERE id = ?",  # nosec B608
            tuple(values),
        )

        if is_bundled_external_mcp_server(name):
            self.normalize_bundled_servers([name])
        return self.get_server(name, project_id=project_id)

    def remove_server(self, name: str, project_id: str) -> bool:
        """
        Remove server by name (cascades to tools). Case-insensitive.

        Args:
            name: Server name
            project_id: Required project ID
        """
        name = name.lower()
        if is_bundled_external_mcp_server(name):
            cursor = self.db.execute(
                "DELETE FROM mcp_servers WHERE name = ?",
                (name,),
            )
            return cursor.rowcount > 0

        cursor = self.db.execute(
            "DELETE FROM mcp_servers WHERE name = ? AND project_id = ?",
            (name, project_id),
        )
        return cursor.rowcount > 0

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
        server = self.get_server(server_name, project_id=project_id)
        if not server:
            logger.warning(f"Server not found: {server_name}")
            return 0

        # Delete existing tools
        self.db.execute("DELETE FROM tools WHERE mcp_server_id = ?", (server.id,))

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
                VALUES (?, ?, ?, ?, ?, ?, ?)
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
        server = self.get_server(server_name, project_id=project_id)
        if not server:
            return []

        rows = self.db.fetchall(
            "SELECT * FROM tools WHERE mcp_server_id = ? ORDER BY name",
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

        server = self.get_server(server_name, project_id=project_id)
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
                    VALUES (?, ?, ?, ?, ?, ?, ?)
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
                    SET description = ?, input_schema = ?, updated_at = ?
                    WHERE id = ?
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
            self.db.execute("DELETE FROM tools WHERE id = ?", (existing.id,))
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
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to read {path}: {e}")
            return 0

        imported = 0

        # Handle Gobby format: {"servers": [{"name": "...", ...}, ...]}
        if "servers" in data and isinstance(data["servers"], list):
            for config in data["servers"]:
                name = config.get("name")
                if not name:
                    continue

                transport = config.get("transport", "stdio")
                self.upsert(
                    name=name,
                    transport=transport,
                    url=config.get("url"),
                    command=config.get("command"),
                    args=config.get("args"),
                    env=config.get("env"),
                    headers=config.get("headers"),
                    enabled=config.get("enabled", True),
                    description=config.get("description"),
                    project_id=project_id,
                )
                imported += 1

        # Handle Claude Code format: {"mcpServers": {"server_name": {...}, ...}}
        elif "mcpServers" in data and isinstance(data["mcpServers"], dict):
            for name, config in data["mcpServers"].items():
                transport = config.get("transport", "stdio")
                self.upsert(
                    name=name,
                    transport=transport,
                    url=config.get("url"),
                    command=config.get("command"),
                    args=config.get("args"),
                    env=config.get("env"),
                    headers=config.get("headers"),
                    enabled=config.get("enabled", True),
                    description=config.get("description"),
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

        # Iterate through server directories
        for server_dir in tools_dir.iterdir():
            if not server_dir.is_dir() or server_dir.name.startswith("."):
                continue

            server_name = server_dir.name

            # Check if server exists in database for this project
            server = self.get_server(server_name, project_id=project_id)
            if not server:
                logger.debug(f"Skipping tools for unknown server: {server_name}")
                continue

            # Collect all tool schemas for this server
            tools = []
            for tool_file in server_dir.glob("*.json"):
                try:
                    with open(tool_file) as f:
                        tool_data = json.load(f)
                    tools.append(
                        {
                            "name": tool_data.get("name", tool_file.stem),
                            "description": tool_data.get("description"),
                            "inputSchema": tool_data.get("inputSchema", {}),
                        }
                    )
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Failed to read tool file {tool_file}: {e}")
                    continue

            # Cache tools to database
            if tools:
                count = self.cache_tools(server_name, tools, project_id=project_id)
                total_imported += count
                logger.info(f"Imported {count} tools for server '{server_name}'")

        return total_imported
