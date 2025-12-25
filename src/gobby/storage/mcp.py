"""Local MCP server and tool storage manager."""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from gobby.storage.database import LocalDatabase

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

    def __init__(self, db: LocalDatabase):
        """Initialize with database connection."""
        self.db = db

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
        # Normalize server name to lowercase
        name = name.lower()

        server_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

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

        return self.get_server(name, project_id=project_id)  # type: ignore

    def get_server(self, name: str, project_id: str) -> MCPServer | None:
        """
        Get server by name (case-insensitive lookup).

        Args:
            name: Server name
            project_id: Required project ID
        """
        # Normalize to lowercase for lookup
        name = name.lower()

        row = self.db.fetchone(
            "SELECT * FROM mcp_servers WHERE name = ? AND project_id = ?",
            (name, project_id),
        )

        return MCPServer.from_row(row) if row else None

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
        query = f"SELECT * FROM mcp_servers WHERE {where_clause} ORDER BY name"
        rows = self.db.fetchall(query, tuple(params))

        return [MCPServer.from_row(row) for row in rows]

    def update_server(
        self, name: str, project_id: str, **fields: Any
    ) -> MCPServer | None:
        """
        Update server fields.

        Args:
            name: Server name
            project_id: Required project ID
        """
        server = self.get_server(name, project_id=project_id)
        if not server:
            return None

        allowed = {"transport", "url", "command", "args", "env", "headers", "enabled", "description"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return server

        # Serialize JSON fields
        if "args" in fields and fields["args"] is not None:
            fields["args"] = json.dumps(fields["args"])
        if "env" in fields and fields["env"] is not None:
            fields["env"] = json.dumps(fields["env"])
        if "headers" in fields and fields["headers"] is not None:
            fields["headers"] = json.dumps(fields["headers"])
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0

        fields["updated_at"] = datetime.utcnow().isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        # Update by server ID to be precise
        values = list(fields.values()) + [server.id]

        self.db.execute(
            f"UPDATE mcp_servers SET {set_clause} WHERE id = ?",
            tuple(values),
        )

        return self.get_server(name, project_id=project_id)

    def remove_server(self, name: str, project_id: str) -> bool:
        """
        Remove server by name (cascades to tools). Case-insensitive.

        Args:
            name: Server name
            project_id: Required project ID
        """
        name = name.lower()
        cursor = self.db.execute(
            "DELETE FROM mcp_servers WHERE name = ? AND project_id = ?",
            (name, project_id),
        )
        return cursor.rowcount > 0  # type: ignore[no-any-return]

    def cache_tools(
        self, server_name: str, tools: list[dict[str, Any]], project_id: str
    ) -> int:
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
        now = datetime.utcnow().isoformat()
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
                    tools.append({
                        "name": tool_data.get("name", tool_file.stem),
                        "description": tool_data.get("description"),
                        "inputSchema": tool_data.get("inputSchema", {}),
                    })
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Failed to read tool file {tool_file}: {e}")
                    continue

            # Cache tools to database
            if tools:
                count = self.cache_tools(server_name, tools, project_id=project_id)
                total_imported += count
                logger.info(f"Imported {count} tools for server '{server_name}'")

        return total_imported
