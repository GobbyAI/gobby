"""MCP server storage operations."""

import json
import uuid
from collections.abc import Iterable
from typing import Any

from gobby.mcp_proxy.bundled import (
    BUNDLED_EXTERNAL_MCP_SERVER_NAMES,
    canonical_project_id_for_server,
    is_bundled_external_mcp_server,
    normalize_bundled_managed_args,
)
from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp_models import MCPServer, Tool
from gobby.storage.mcp_secrets import cleanup_replaced_mcp_secrets, protect_mcp_mapping
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore
from gobby.utils.datetime import utc_now


def _parse_mcp_bool(value: Any, *, field_name: str, allow_none: bool = False) -> bool | None:
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"{field_name} must be a boolean")
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be a boolean")


class MCPServerStorageMixin:
    """MCP server persistence, lookup, listing, and normalization methods."""

    db: HubDatabase

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
        requires_oauth: bool | None = None,
        oauth_provider: str | None = None,
        connect_timeout: float | None = None,
    ) -> MCPServer:
        """Persist a server row without applying bundled-server cleanup."""
        server_id = str(uuid.uuid4())
        requires_oauth_value = _parse_mcp_bool(
            requires_oauth,
            field_name="requires_oauth",
            allow_none=True,
        )
        if requires_oauth_value is False:
            oauth_provider = None
        secret_store = SecretStore(self.db)
        existing = self.get_server(name, project_id=project_id)
        with self.db.transaction():
            protected_env = protect_mcp_mapping(
                env,
                secret_store=secret_store,
                persistence="database",
                scope=project_id,
                server_name=name,
                field="env",
            )
            protected_headers = protect_mcp_mapping(
                headers,
                secret_store=secret_store,
                persistence="database",
                scope=project_id,
                server_name=name,
                field="headers",
            )
            self.db.execute(
                """
                INSERT INTO mcp_servers (
                    id, name, project_id, transport, url, command, args, env, headers,
                    enabled, description, requires_oauth, oauth_provider, connect_timeout
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(name, project_id) DO UPDATE SET
                    transport = excluded.transport,
                    url = excluded.url,
                    command = excluded.command,
                    args = excluded.args,
                    env = excluded.env,
                    headers = excluded.headers,
                    enabled = excluded.enabled,
                    description = COALESCE(excluded.description, mcp_servers.description),
                    requires_oauth = COALESCE(excluded.requires_oauth, mcp_servers.requires_oauth),
                    oauth_provider = CASE
                        WHEN COALESCE(excluded.requires_oauth, mcp_servers.requires_oauth) = FALSE
                        THEN NULL
                        ELSE COALESCE(excluded.oauth_provider, mcp_servers.oauth_provider)
                    END,
                    connect_timeout = COALESCE(excluded.connect_timeout, mcp_servers.connect_timeout),
                    updated_at = excluded.updated_at
                """,
                (
                    server_id,
                    name,
                    project_id,
                    transport,
                    url,
                    command,
                    json.dumps(args) if args is not None else None,
                    json.dumps(protected_env) if protected_env is not None else None,
                    json.dumps(protected_headers) if protected_headers is not None else None,
                    _parse_mcp_bool(enabled, field_name="enabled"),
                    description,
                    requires_oauth_value,
                    oauth_provider,
                    connect_timeout,
                ),
            )
            cleanup_replaced_mcp_secrets(
                secret_store,
                persistence="database",
                scope=project_id,
                server_name=name,
                old_env=existing.env if existing else None,
                old_headers=existing.headers if existing else None,
                new_env=protected_env,
                new_headers=protected_headers,
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

        lookup_ids = [GLOBAL_PROJECT_ID]
        if project_id not in lookup_ids:
            lookup_ids.append(project_id)
        return lookup_ids

    def _fetch_servers_by_name(self, name: str) -> list[MCPServer]:
        """Fetch every row for a server name across projects."""
        rows = self.db.fetchall(
            "SELECT * FROM mcp_servers WHERE name = %s ORDER BY updated_at DESC, created_at DESC",
            (name,),
        )
        return [MCPServer.from_row(row) for row in rows]

    def _load_tools_for_server_id(self, server_id: str) -> list[Tool]:
        """Load cached tools directly by server ID."""
        rows = self.db.fetchall(
            "SELECT * FROM tools WHERE mcp_server_id = %s ORDER BY name",
            (server_id,),
        )
        return [Tool.from_row(row) for row in rows]

    def _replace_tools_for_server_id(
        self,
        conn: Any,
        server_id: str,
        tools: list[Tool],
    ) -> None:
        """Replace cached tools for a server ID inside the caller's transaction."""
        generation_state = EmbeddingGenerationState(self.db)
        stale_rows = conn.execute(
            "SELECT id FROM tools WHERE mcp_server_id = %s", (server_id,)
        ).fetchall()
        conn.execute("DELETE FROM tools WHERE mcp_server_id = %s", (server_id,))
        for stale_row in stale_rows:
            generation_state.append_change(
                "tool", str(stale_row["id"]), is_tombstone=True, transaction=conn
            )
        if not tools:
            return

        for tool in tools:
            tool_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO tools (id, mcp_server_id, name, description, input_schema)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    tool_id,
                    server_id,
                    tool.name,
                    tool.description,
                    json.dumps(tool.input_schema) if tool.input_schema is not None else None,
                ),
            )
            generation_state.append_change("tool", tool_id, transaction=conn)

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
        for server in servers:
            if server.project_id == GLOBAL_PROJECT_ID:
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
        requires_oauth: bool | None = None,
        oauth_provider: str | None = None,
        connect_timeout: float | None = None,
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
        sanitized_args = (
            normalize_bundled_managed_args(name, args)
            if args is not None or is_bundled_external_mcp_server(name)
            else None
        )

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
            requires_oauth=requires_oauth,
            oauth_provider=oauth_provider,
            connect_timeout=connect_timeout,
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
                "SELECT * FROM mcp_servers WHERE name = %s AND project_id = %s",
                (name, lookup_project_id),
            )
            if row:
                return MCPServer.from_row(row)

        return None

    def get_server_by_id(self, server_id: str) -> MCPServer | None:
        """Get server by ID."""
        row = self.db.fetchone("SELECT * FROM mcp_servers WHERE id = %s", (server_id,))
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
        conditions = ["project_id = %s"]
        params: list[Any] = [project_id]

        if enabled_only:
            conditions.append("enabled IS TRUE")

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
            query = "SELECT * FROM mcp_servers WHERE enabled IS TRUE ORDER BY name"
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
        conditions = ["enabled IS TRUE"] if enabled_only else []
        project_where = " AND ".join(["project_id = %s"] + conditions) or "project_id = %s"
        project_rows = self.db.fetchall(
            f"SELECT * FROM mcp_servers WHERE {project_where} ORDER BY name",  # nosec B608
            (project_id,),
        )

        bundled_groups: dict[str, list[MCPServer]] = {}
        runtime_servers: list[MCPServer] = []
        for row in project_rows:
            server = MCPServer.from_row(row)
            if is_bundled_external_mcp_server(server.name):
                bundled_groups.setdefault(server.name, []).append(server)
            else:
                runtime_servers.append(server)

        global_conditions = ["project_id = %s"]
        params: list[Any] = [GLOBAL_PROJECT_ID]
        if enabled_only:
            global_conditions.append("enabled IS TRUE")
        global_conditions.append(
            "name IN ({})".format(",".join("%s" for _ in BUNDLED_EXTERNAL_MCP_SERVER_NAMES))
        )
        params.extend(sorted(BUNDLED_EXTERNAL_MCP_SERVER_NAMES))
        rows = self.db.fetchall(
            f"SELECT * FROM mcp_servers WHERE {' AND '.join(global_conditions)} ORDER BY name",  # nosec B608
            tuple(params),
        )
        for row in rows:
            server = MCPServer.from_row(row)
            bundled_groups.setdefault(server.name, []).append(server)

        for _name, servers in sorted(bundled_groups.items()):
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
                    args=normalize_bundled_managed_args(name, canonical_source.args),
                    env=canonical_source.env,
                    headers=canonical_source.headers,
                    enabled=canonical_source.enabled,
                    description=canonical_source.description,
                    requires_oauth=canonical_source.requires_oauth,
                    oauth_provider=canonical_source.oauth_provider,
                    connect_timeout=canonical_source.connect_timeout,
                )

                if tools_to_preserve and (
                    len(servers) > 1 or canonical_source.id != canonical_server.id
                ):
                    self._replace_tools_for_server_id(conn, canonical_server.id, tools_to_preserve)
                    stats["tools_migrated"] += len(tools_to_preserve)

                duplicate_ids = [
                    server.id for server in servers if server.id != canonical_server.id
                ]
                generation_state = EmbeddingGenerationState(self.db)
                for server_id in duplicate_ids:
                    stale_tool_rows = conn.execute(
                        "SELECT id FROM tools WHERE mcp_server_id = %s", (server_id,)
                    ).fetchall()
                    for stale_row in stale_tool_rows:
                        generation_state.append_change(
                            "tool", str(stale_row["id"]), is_tombstone=True, transaction=conn
                        )
                    conn.execute("DELETE FROM mcp_servers WHERE id = %s", (server_id,))

            if (
                canonical_source.project_id != GLOBAL_PROJECT_ID
                or normalize_bundled_managed_args(name, canonical_source.args)
                != canonical_source.args
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
        project_id = canonical_project_id_for_server(name, project_id)
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
            "requires_oauth",
            "oauth_provider",
            "connect_timeout",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return server

        if "args" in fields:
            fields["args"] = normalize_bundled_managed_args(name, fields["args"])

        secret_store = SecretStore(self.db)
        with self.db.transaction():
            protected_env = server.env
            protected_headers = server.headers
            if "env" in fields:
                protected_env = protect_mcp_mapping(
                    fields["env"],
                    secret_store=secret_store,
                    persistence="database",
                    scope=project_id,
                    server_name=name,
                    field="env",
                )
                fields["env"] = protected_env
            if "headers" in fields:
                protected_headers = protect_mcp_mapping(
                    fields["headers"],
                    secret_store=secret_store,
                    persistence="database",
                    scope=project_id,
                    server_name=name,
                    field="headers",
                )
                fields["headers"] = protected_headers

            if "args" in fields and fields["args"] is not None:
                fields["args"] = json.dumps(fields["args"])
            if "env" in fields and fields["env"] is not None:
                fields["env"] = json.dumps(fields["env"])
            if "headers" in fields and fields["headers"] is not None:
                fields["headers"] = json.dumps(fields["headers"])
            if "enabled" in fields:
                fields["enabled"] = _parse_mcp_bool(fields["enabled"], field_name="enabled")
            if "requires_oauth" in fields:
                fields["requires_oauth"] = _parse_mcp_bool(
                    fields["requires_oauth"],
                    field_name="requires_oauth",
                    allow_none=True,
                )
                if fields["requires_oauth"] is False:
                    fields["oauth_provider"] = None
            if "connect_timeout" in fields and fields["connect_timeout"] is not None:
                fields["connect_timeout"] = float(fields["connect_timeout"])

            fields["updated_at"] = utc_now()
            set_clause = ", ".join(f"{k} = %s" for k in fields)
            values = list(fields.values()) + [server.id]
            self.db.execute(
                f"UPDATE mcp_servers SET {set_clause} WHERE id = %s",  # nosec B608
                tuple(values),
            )
            cleanup_replaced_mcp_secrets(
                secret_store,
                persistence="database",
                scope=project_id,
                server_name=name,
                old_env=server.env,
                old_headers=server.headers,
                new_env=protected_env,
                new_headers=protected_headers,
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
        servers = (
            self._fetch_servers_by_name(name)
            if is_bundled_external_mcp_server(name)
            else [server]
            if (server := self.get_server(name, project_id=project_id)) is not None
            else []
        )
        if not servers:
            return False

        secret_store = SecretStore(self.db)
        generation_state = EmbeddingGenerationState(self.db)
        with self.db.transaction() as conn:
            for server in servers:
                stale_tool_rows = conn.execute(
                    "SELECT id FROM tools WHERE mcp_server_id = %s", (server.id,)
                ).fetchall()
                for stale_row in stale_tool_rows:
                    generation_state.append_change(
                        "tool", str(stale_row["id"]), is_tombstone=True, transaction=conn
                    )
            if is_bundled_external_mcp_server(name):
                cursor = conn.execute(
                    "DELETE FROM mcp_servers WHERE name = %s",
                    (name,),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM mcp_servers WHERE name = %s AND project_id = %s",
                    (name, project_id),
                )
            for server in servers:
                cleanup_replaced_mcp_secrets(
                    secret_store,
                    persistence="database",
                    scope=server.project_id,
                    server_name=name,
                    old_env=server.env,
                    old_headers=server.headers,
                    new_env=None,
                    new_headers=None,
                )
            return cursor.rowcount > 0
