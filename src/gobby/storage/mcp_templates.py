"""MCP server template registry storage."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from gobby.storage.definitions._shared import compute_definition_hash
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.mcp_models import MCPServer
from gobby.storage.projects import GLOBAL_PROJECT_ID
from gobby.utils.datetime import normalize_datetime_model


def _loads_json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value:
            return {}
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
        raise ValueError("template definition must be a JSON object")
    raise ValueError("template definition must be a JSON object")


def _definition_hash(definition: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(definition), sort_keys=True, separators=(",", ":"))
    return compute_definition_hash(encoded)


def _initial_enabled(definition: Mapping[str, Any], enabled: bool | None) -> bool:
    if enabled is not None:
        return bool(enabled)
    raw = definition.get("enabled", True)
    return bool(raw)


class _ServerById(Protocol):
    def get_server_by_id(self, server_id: str) -> MCPServer | None: ...


@normalize_datetime_model(required=("created_at", "updated_at"))
@dataclass(frozen=True)
class MCPServerTemplateRow:
    """Persisted MCP server template."""

    id: str
    name: str
    project_id: str
    owner: str
    source_path: str | None
    definition: dict[str, Any]
    definition_hash: str
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> MCPServerTemplateRow:
        return cls(
            id=str(row["id"]),
            name=row["name"],
            project_id=str(row["project_id"]),
            owner=row["owner"],
            source_path=row["source_path"],
            definition=_loads_json_object(row["definition"]),
            definition_hash=row["definition_hash"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "project_id": self.project_id,
            "owner": self.owner,
            "source_path": self.source_path,
            "definition": self.definition,
            "definition_hash": self.definition_hash,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class MCPTemplateStorageMixin:
    """CRUD for `mcp_server_templates`, keyed by `(name, project_id)`."""

    db: HubDatabase

    def upsert_template(
        self,
        *,
        name: str,
        project_id: str,
        owner: str,
        definition: Mapping[str, Any],
        source_path: str | None = None,
        enabled: bool | None = None,
    ) -> MCPServerTemplateRow:
        definition_map = dict(definition)
        digest = _definition_hash(definition_map)
        definition_json = json.dumps(definition_map)
        with self.db.transaction() as conn:
            existing = conn.execute(
                """
                SELECT * FROM mcp_server_templates
                WHERE name = %s AND project_id = %s
                FOR UPDATE
                """,
                (name, project_id),
            ).fetchone()
            if existing is None:
                template_id = str(uuid.uuid4())
                stored_enabled = _initial_enabled(definition_map, enabled)
                conn.execute(
                    """
                    INSERT INTO mcp_server_templates (
                        id, name, project_id, owner, source_path, definition,
                        definition_hash, enabled
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        template_id,
                        name,
                        project_id,
                        owner,
                        source_path,
                        definition_json,
                        digest,
                        stored_enabled,
                    ),
                )
            else:
                current = MCPServerTemplateRow.from_row(existing)
                next_enabled = current.enabled if enabled is None else bool(enabled)
                next_source = source_path if source_path is not None else current.source_path
                next_definition = current.definition
                next_hash = current.definition_hash
                drifted = digest != current.definition_hash
                if current.owner == "gobby":
                    if drifted:
                        next_definition = definition_map
                        next_hash = digest
                else:
                    next_definition = definition_map
                    next_hash = digest
                unchanged = (
                    next_hash == current.definition_hash
                    and next_source == current.source_path
                    and next_enabled == current.enabled
                    and owner == current.owner
                )
                if not unchanged:
                    conn.execute(
                        """
                        UPDATE mcp_server_templates
                        SET definition = %s,
                            definition_hash = %s,
                            source_path = %s,
                            enabled = %s,
                            owner = %s,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            json.dumps(next_definition),
                            next_hash,
                            next_source,
                            next_enabled,
                            owner,
                            current.id,
                        ),
                    )
        row = self.get_template(name, project_id=project_id)
        if row is None or row.project_id != project_id:
            raise RuntimeError(f"Failed to retrieve template '{name}' after upsert")
        return row

    def get_template(self, name: str, *, project_id: str) -> MCPServerTemplateRow | None:
        row = self.db.fetchone(
            """
            SELECT * FROM mcp_server_templates
            WHERE name = %s AND project_id = %s
            """,
            (name, project_id),
        )
        if row is not None:
            return MCPServerTemplateRow.from_row(row)
        if project_id == GLOBAL_PROJECT_ID:
            return None
        global_row = self.db.fetchone(
            """
            SELECT * FROM mcp_server_templates
            WHERE name = %s AND project_id = %s
            """,
            (name, GLOBAL_PROJECT_ID),
        )
        return MCPServerTemplateRow.from_row(global_row) if global_row is not None else None

    def get_template_by_id(self, template_id: str) -> MCPServerTemplateRow | None:
        row = self.db.fetchone(
            "SELECT * FROM mcp_server_templates WHERE id = %s",
            (template_id,),
        )
        return MCPServerTemplateRow.from_row(row) if row is not None else None

    def list_templates(
        self,
        *,
        project_id: str,
        enabled_only: bool = True,
    ) -> list[MCPServerTemplateRow]:
        rows = self.db.fetchall(
            """
            SELECT * FROM mcp_server_templates
            WHERE project_id = %s OR project_id = %s
            """,
            (project_id, GLOBAL_PROJECT_ID),
        )
        by_name: dict[str, MCPServerTemplateRow] = {}
        ordered = sorted(
            rows,
            key=lambda row: 0 if str(row["project_id"]) == GLOBAL_PROJECT_ID else 1,
        )
        for raw in ordered:
            item = MCPServerTemplateRow.from_row(raw)
            by_name[item.name] = item
        items = list(by_name.values())
        if enabled_only:
            items = [item for item in items if item.enabled]
        items.sort(key=lambda item: item.name)
        return items

    def delete_template(self, name: str, *, project_id: str) -> bool:
        cursor = self.db.execute(
            "DELETE FROM mcp_server_templates WHERE name = %s AND project_id = %s",
            (name, project_id),
        )
        return cursor.rowcount > 0

    def list_template_instances(self, template_id: str) -> list[MCPServer]:
        rows = self.db.fetchall(
            "SELECT id FROM mcp_servers WHERE template_id = %s ORDER BY name",
            (template_id,),
        )
        lookup = cast(_ServerById, self)
        servers: list[MCPServer] = []
        for row in rows:
            server = lookup.get_server_by_id(str(row["id"]))
            if server is not None:
                servers.append(server)
        return servers
