"""MCP server storage operations."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Protocol, cast

from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp_models import MCPServer, Tool
from gobby.storage.mcp_secrets import cleanup_replaced_mcp_secrets, protect_mcp_mapping
from gobby.storage.mcp_templates import MCPServerTemplateRow
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.storage.secrets import SecretStore
from gobby.utils.datetime import utc_now

_SERVER_SELECT = """
SELECT mcp_servers.*, mcp_server_templates.name AS template
FROM mcp_servers
LEFT JOIN mcp_server_templates ON mcp_server_templates.id = mcp_servers.template_id
"""

_TEMPLATE_OWNED_FIELDS = (
    "transport",
    "url",
    "command",
    "args",
    "env",
    "headers",
    "connect_timeout",
    "runtime_hook",
)


class _TemplateLookup(Protocol):
    def get_template_by_id(self, template_id: str) -> MCPServerTemplateRow | None: ...


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


def _json_param(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


class MCPServerStorageMixin:
    """MCP server persistence, lookup, listing, and template refresh methods."""

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
        template_id: str | None = None,
        template_values: dict[str, Any] | None = None,
        runtime_hook: str | None = None,
    ) -> MCPServer:
        """Persist a server row for the exact `(name, project_id)` key."""
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
                    enabled, description, requires_oauth, oauth_provider, connect_timeout,
                    template_id, template_values, runtime_hook
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    template_id = COALESCE(excluded.template_id, mcp_servers.template_id),
                    template_values = COALESCE(excluded.template_values, mcp_servers.template_values),
                    runtime_hook = COALESCE(excluded.runtime_hook, mcp_servers.runtime_hook),
                    updated_at = now()
                """,
                (
                    server_id,
                    name,
                    project_id,
                    transport,
                    url,
                    command,
                    _json_param(args),
                    _json_param(protected_env),
                    _json_param(protected_headers),
                    _parse_mcp_bool(enabled, field_name="enabled"),
                    description,
                    requires_oauth_value,
                    oauth_provider,
                    connect_timeout,
                    template_id,
                    _json_param(template_values),
                    runtime_hook,
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
        template_id: str | None = None,
        template_values: dict[str, Any] | None = None,
        runtime_hook: str | None = None,
    ) -> MCPServer:
        """Insert or update an MCP server for `(name, project_id)`."""
        name = name.lower()
        return self._persist_server(
            name=name,
            transport=transport,
            project_id=project_id,
            url=url,
            command=command,
            args=args,
            env=env,
            headers=headers,
            enabled=enabled,
            description=description,
            requires_oauth=requires_oauth,
            oauth_provider=oauth_provider,
            connect_timeout=connect_timeout,
            template_id=template_id,
            template_values=template_values,
            runtime_hook=runtime_hook,
        )

    def insert_server(
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
        template_id: str | None = None,
        template_values: dict[str, Any] | None = None,
        runtime_hook: str | None = None,
    ) -> MCPServer | None:
        """Create a server only when `(name, project_id)` is free.

        Conflict is the database outcome: env/headers stay empty until INSERT
        returns an id, so a losing create writes neither a row nor a secret slot.
        """
        name = name.lower()
        server_id = str(uuid.uuid4())
        requires_oauth_value = _parse_mcp_bool(
            requires_oauth,
            field_name="requires_oauth",
            allow_none=True,
        )
        if requires_oauth_value is False:
            oauth_provider = None
        secret_store = SecretStore(self.db)
        with self.db.transaction() as conn:
            inserted = conn.execute(
                """
                INSERT INTO mcp_servers (
                    id, name, project_id, transport, url, command, args, env, headers,
                    enabled, description, requires_oauth, oauth_provider, connect_timeout,
                    template_id, template_values, runtime_hook
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (name, project_id) DO NOTHING
                RETURNING id
                """,
                (
                    server_id,
                    name,
                    project_id,
                    transport,
                    url,
                    command,
                    _json_param(args),
                    "{}",
                    "{}",
                    _parse_mcp_bool(enabled, field_name="enabled"),
                    description,
                    requires_oauth_value,
                    oauth_provider,
                    connect_timeout,
                    template_id,
                    _json_param(template_values),
                    runtime_hook,
                ),
            ).fetchone()
            if inserted is None:
                return None
            created_id = str(inserted["id"])
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
            conn.execute(
                """
                UPDATE mcp_servers
                SET env = %s, headers = %s, updated_at = now()
                WHERE id = %s
                """,
                (_json_param(protected_env), _json_param(protected_headers), created_id),
            )
        return self.get_server_by_id(created_id)

    def get_server(self, name: str, project_id: str) -> MCPServer | None:
        """Get the exact `(name, project_id)` row."""
        name = name.lower()
        row = self.db.fetchone(
            f"{_SERVER_SELECT} WHERE mcp_servers.name = %s AND mcp_servers.project_id = %s",
            (name, project_id),
        )
        return MCPServer.from_row(row) if row else None

    def get_server_by_id(self, server_id: str) -> MCPServer | None:
        """Get server by ID, joining the template name when present."""
        row = self.db.fetchone(
            f"{_SERVER_SELECT} WHERE mcp_servers.id = %s",
            (server_id,),
        )
        return MCPServer.from_row(row) if row else None

    def resolve_server(self, name: str, *, project_id: str) -> MCPServer | None:
        """Resolve project-first, then the global row; shadowing ignores enabled."""
        exact = self.get_server(name, project_id=project_id)
        if exact is not None:
            return exact
        if project_id == GLOBAL_PROJECT_ID:
            return None
        return self.get_server(name, project_id=GLOBAL_PROJECT_ID)

    def list_servers(
        self,
        project_id: str,
        enabled_only: bool = True,
    ) -> list[MCPServer]:
        """List MCP servers for a project."""
        conditions = ["mcp_servers.project_id = %s"]
        params: list[Any] = [project_id]
        if enabled_only:
            conditions.append("mcp_servers.enabled IS TRUE")
        where_clause = " AND ".join(conditions)
        query = f"{_SERVER_SELECT} WHERE {where_clause} ORDER BY mcp_servers.name"  # nosec B608
        rows = self.db.fetchall(query, tuple(params))
        return [MCPServer.from_row(row) for row in rows]

    def list_all_servers(self, enabled_only: bool = True) -> list[MCPServer]:
        """List all MCP servers across all projects."""
        if enabled_only:
            query = f"{_SERVER_SELECT} WHERE mcp_servers.enabled IS TRUE ORDER BY mcp_servers.name"
        else:
            query = f"{_SERVER_SELECT} ORDER BY mcp_servers.name"
        rows = self.db.fetchall(query, ())
        return [MCPServer.from_row(row) for row in rows]

    def list_runtime_servers(self, project_id: str, enabled_only: bool = True) -> list[MCPServer]:
        """List project rows union global rows, with project names shadowing global."""
        rows = self.db.fetchall(
            f"""
            {_SERVER_SELECT}
            WHERE mcp_servers.project_id = %s OR mcp_servers.project_id = %s
            """,
            (project_id, GLOBAL_PROJECT_ID),
        )
        by_name: dict[str, MCPServer] = {}
        ordered = sorted(
            rows,
            key=lambda row: 0 if str(row["project_id"]) == GLOBAL_PROJECT_ID else 1,
        )
        for raw in ordered:
            server = MCPServer.from_row(raw)
            by_name[server.name] = server
        servers = list(by_name.values())
        if enabled_only:
            servers = [server for server in servers if server.enabled]
        servers.sort(key=lambda server: server.name)
        return servers

    def refresh_template_instances(
        self,
        expand: Callable[[MCPServerTemplateRow, MCPServer], Mapping[str, Any]],
        *,
        server_id: str | None = None,
    ) -> dict[str, Any]:
        """Re-expand template-owned fields; leave instance-owned fields untouched."""
        if server_id is not None:
            target = self.get_server_by_id(server_id)
            servers = [target] if target is not None and target.template_id else []
        else:
            rows = self.db.fetchall(f"{_SERVER_SELECT} WHERE mcp_servers.template_id IS NOT NULL")
            servers = [MCPServer.from_row(row) for row in rows]

        templates = cast(_TemplateLookup, self)
        refreshed = 0
        errors: dict[str, dict[str, str]] = {}
        for server in servers:
            if not server.template_id:
                continue
            template = templates.get_template_by_id(server.template_id)
            if template is None:
                continue
            try:
                expanded = expand(template, server)
            except ValueError as exc:
                errors[str(server.id)] = {
                    "name": server.name,
                    "project_id": str(server.project_id),
                    "error": str(exc),
                }
                continue
            fields: dict[str, Any] = {}
            for key in _TEMPLATE_OWNED_FIELDS:
                if key not in expanded:
                    continue
                if expanded[key] != getattr(server, key):
                    fields[key] = expanded[key]
            if fields:
                self.update_server(server.name, project_id=server.project_id, **fields)
            refreshed += 1
        return {"refreshed": refreshed, "errors": errors}

    def update_server(self, name: str, project_id: str, **fields: Any) -> MCPServer | None:
        """Update server fields on the exact `(name, project_id)` row."""
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
            "requires_oauth",
            "oauth_provider",
            "connect_timeout",
            "template_id",
            "template_values",
            "runtime_hook",
        }
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return server

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
            if "template_values" in fields and fields["template_values"] is not None:
                fields["template_values"] = json.dumps(fields["template_values"])
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
            return self.get_server(name, project_id=project_id)

    def remove_server(self, name: str, project_id: str) -> bool:
        """Remove the exact `(name, project_id)` row (cascades to tools)."""
        name = name.lower()
        server = self.get_server(name, project_id=project_id)
        if server is None:
            return False

        secret_store = SecretStore(self.db)
        generation_state = EmbeddingGenerationState(self.db)
        with self.db.transaction() as conn:
            stale_tool_rows = conn.execute(
                "SELECT id FROM tools WHERE mcp_server_id = %s", (server.id,)
            ).fetchall()
            for stale_row in stale_tool_rows:
                generation_state.append_change(
                    "tool", str(stale_row["id"]), is_tombstone=True, transaction=conn
                )
            cursor = conn.execute(
                "DELETE FROM mcp_servers WHERE name = %s AND project_id = %s",
                (name, project_id),
            )
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
