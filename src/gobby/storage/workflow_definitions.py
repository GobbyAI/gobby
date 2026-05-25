"""Workflow definition storage manager for local database."""

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import json_text_expr, older_than_now_expr

logger = logging.getLogger(__name__)

DefinitionSource = Literal["installed", "template", "agent", "project", "custom"]
_WORKFLOW_DEFINITIONS_REVISION = 0
_WORKFLOW_DEFINITIONS_REVISION_LOCK = Lock()


def compute_definition_hash(definition_json: str) -> str:
    """Compute a SHA-256 hash of a definition JSON string.

    Used for cheap drift detection between installed definitions
    and their on-disk template files.
    """
    return hashlib.sha256(definition_json.encode()).hexdigest()


def get_workflow_definitions_revision() -> int:
    """Return the in-process workflow-definition mutation revision."""
    with _WORKFLOW_DEFINITIONS_REVISION_LOCK:
        return _WORKFLOW_DEFINITIONS_REVISION


def _bump_workflow_definitions_revision() -> None:
    global _WORKFLOW_DEFINITIONS_REVISION
    with _WORKFLOW_DEFINITIONS_REVISION_LOCK:
        _WORKFLOW_DEFINITIONS_REVISION += 1

    from gobby.hooks.session_activation import clear_active_rule_names_cache

    clear_active_rule_names_cache()


@dataclass
class WorkflowDefinitionRow:
    """Represents a workflow definition row from the database."""

    id: str
    name: str
    workflow_type: str
    enabled: bool
    priority: int
    definition_json: str
    source: DefinitionSource
    created_at: str
    updated_at: str
    project_id: str | None = None
    description: str | None = None
    version: str = "1.0"
    sources: list[str] | None = None
    tags: list[str] | None = None
    canvas_json: str | None = None
    deleted_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "WorkflowDefinitionRow":
        def _parse_json_list(val: str | None) -> list[str] | None:
            if val is None:
                return None
            try:
                result = json.loads(val)
                return result if isinstance(result, list) else None
            except (json.JSONDecodeError, TypeError):
                return None

        return cls(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            workflow_type=row["workflow_type"],
            version=row["version"] or "1.0",
            enabled=bool(row["enabled"]),
            priority=int(row["priority"]) if row["priority"] else 100,
            sources=_parse_json_list(row["sources"]),
            definition_json=row["definition_json"],
            canvas_json=row["canvas_json"],
            source=row["source"] or "installed",
            tags=_parse_json_list(row["tags"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"] if "deleted_at" in row.keys() else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "workflow_type": self.workflow_type,
            "version": self.version,
            "enabled": self.enabled,
            "priority": self.priority,
            "sources": self.sources,
            "definition_json": self.definition_json,
            "canvas_json": self.canvas_json,
            "source": self.source,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "deleted_at": self.deleted_at,
        }


class LocalWorkflowDefinitionManager:
    """Manages workflow definitions in the local database."""

    def __init__(self, db: HubDatabase):
        self.db = db

    def create(
        self,
        name: str,
        definition_json: str,
        workflow_type: str = "workflow",
        project_id: str | None = None,
        description: str | None = None,
        version: str = "1.0",
        enabled: bool = True,
        priority: int = 100,
        sources: list[str] | None = None,
        canvas_json: str | None = None,
        source: DefinitionSource = "installed",
        tags: list[str] | None = None,
    ) -> WorkflowDefinitionRow:
        """Create a new workflow definition in the database."""
        definition_id = str(uuid4())
        now = datetime.now(UTC).isoformat()

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO workflow_definitions (
                    id, project_id, name, description, workflow_type,
                    version, enabled, priority, sources,
                    definition_json, canvas_json, source, tags,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition_id,
                    project_id,
                    name,
                    description,
                    workflow_type,
                    version,
                    bool(enabled),
                    priority,
                    json.dumps(sources) if sources else None,
                    definition_json,
                    canvas_json,
                    source,
                    json.dumps(tags) if tags else None,
                    now,
                    now,
                ),
            )

        _bump_workflow_definitions_revision()
        return self.get(definition_id)

    def get(self, definition_id: str, include_deleted: bool = False) -> WorkflowDefinitionRow:
        """Get a workflow definition by primary key."""
        sql = "SELECT * FROM workflow_definitions WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        row = self.db.fetchone(sql, (definition_id,))
        if not row:
            raise ValueError(f"Workflow definition {definition_id} not found")
        return WorkflowDefinitionRow.from_row(row)

    def get_by_name(
        self,
        name: str,
        project_id: str | None = None,
        include_deleted: bool = False,
    ) -> WorkflowDefinitionRow | None:
        """Get a workflow definition by name (project-scoped first, then global fallback)."""
        deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
        if project_id:
            row = self.db.fetchone(
                f"SELECT * FROM workflow_definitions WHERE name = ? AND project_id = ?{deleted_filter}",
                (name, project_id),
            )
            if row:
                return WorkflowDefinitionRow.from_row(row)
        row = self.db.fetchone(
            f"SELECT * FROM workflow_definitions WHERE name = ? AND project_id IS NULL{deleted_filter}",
            (name,),
        )
        return WorkflowDefinitionRow.from_row(row) if row else None

    def update(self, definition_id: str, **fields: Any) -> WorkflowDefinitionRow:
        """Partial update of a workflow definition."""
        json_fields = {"sources", "tags"}
        values: dict[str, Any] = {}
        for key, val in fields.items():
            if key in json_fields and val is not None:
                values[key] = json.dumps(val)
            elif key == "enabled" and isinstance(val, bool):
                values[key] = val
            else:
                values[key] = val

        if not values:
            return self.get(definition_id)

        values["updated_at"] = datetime.now(UTC).isoformat()
        cursor = self.db.safe_update("workflow_definitions", values, "id = ?", (definition_id,))
        if cursor.rowcount > 0:
            _bump_workflow_definitions_revision()
        return self.get(definition_id)

    def delete(self, definition_id: str) -> bool:
        """Soft-delete a workflow definition by setting deleted_at."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE workflow_definitions SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, now, definition_id),
            )
            deleted = cursor.rowcount > 0
        if deleted:
            _bump_workflow_definitions_revision()
        return deleted

    def hard_delete(self, definition_id: str) -> bool:
        """Permanently delete a workflow definition from the database."""
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM workflow_definitions WHERE id = ?", (definition_id,))
            deleted = cursor.rowcount > 0
        if deleted:
            _bump_workflow_definitions_revision()
        return deleted

    def restore(self, definition_id: str) -> WorkflowDefinitionRow:
        """Restore a soft-deleted workflow definition."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE workflow_definitions SET deleted_at = NULL, updated_at = ? WHERE id = ? AND deleted_at IS NOT NULL",
                (now, definition_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Workflow definition {definition_id} not found or not deleted")
        _bump_workflow_definitions_revision()
        return self.get(definition_id)

    def purge_deleted(self, older_than_days: int = 30) -> int:
        """Hard-delete rows that were soft-deleted more than older_than_days ago."""
        if older_than_days < 1:
            raise ValueError(f"older_than_days must be >= 1, got {older_than_days}")
        deleted_before_sql = older_than_now_expr(self.db, "deleted_at", "?", "day")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"""
                DELETE FROM workflow_definitions
                WHERE deleted_at IS NOT NULL
                  AND {deleted_before_sql}
                """,  # nosec B608 # cutoff expression is selected by storage dialect.
                (older_than_days,),
            )
            count = cursor.rowcount
        if count:
            _bump_workflow_definitions_revision()
            logger.info(f"Purged {count} soft-deleted workflow definitions")
        return count

    def list_all(
        self,
        project_id: str | None = None,
        workflow_type: str | None = None,
        enabled: bool | None = None,
        include_deleted: bool = False,
    ) -> list[WorkflowDefinitionRow]:
        """List workflow definitions with optional filters."""
        conditions: list[str] = []
        params: list[Any] = []

        if not include_deleted:
            conditions.append("deleted_at IS NULL")

        if project_id:
            conditions.append("(project_id = ? OR project_id IS NULL)")
            params.append(project_id)

        if workflow_type:
            conditions.append("workflow_type = ?")
            params.append(workflow_type)

        if enabled is not None:
            conditions.append("enabled = ?")
            params.append(enabled)

        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.fetchall(
            f"SELECT * FROM workflow_definitions{where} ORDER BY name",
            tuple(params),
        )
        return [WorkflowDefinitionRow.from_row(r) for r in rows]

    def list_rules_by_event(
        self,
        event: str,
        project_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[WorkflowDefinitionRow]:
        """List rule definitions filtered by event type from definition_json."""
        event_sql = json_text_expr(self.db, "definition_json", "event")
        conditions = [
            "workflow_type = 'rule'",
            "deleted_at IS NULL",
            f"{event_sql} = ?",
        ]
        params: list[Any] = [event]

        if project_id:
            conditions.append("(project_id = ? OR project_id IS NULL)")
            params.append(project_id)

        if enabled is not None:
            conditions.append("enabled = ?")
            params.append(enabled)

        where = f" WHERE {' AND '.join(conditions)}"
        rows = self.db.fetchall(
            f"SELECT * FROM workflow_definitions{where} ORDER BY priority, name",
            tuple(params),
        )
        return [WorkflowDefinitionRow.from_row(r) for r in rows]

    def list_rules_by_group(
        self,
        group: str,
        project_id: str | None = None,
        enabled: bool | None = None,
    ) -> list[WorkflowDefinitionRow]:
        """List rule definitions filtered by group from definition_json."""
        group_sql = json_text_expr(self.db, "definition_json", "group")
        conditions = [
            "workflow_type = 'rule'",
            "deleted_at IS NULL",
            f"{group_sql} = ?",
        ]
        params: list[Any] = [group]

        if project_id:
            conditions.append("(project_id = ? OR project_id IS NULL)")
            params.append(project_id)

        if enabled is not None:
            conditions.append("enabled = ?")
            params.append(enabled)

        where = f" WHERE {' AND '.join(conditions)}"
        rows = self.db.fetchall(
            f"SELECT * FROM workflow_definitions{where} ORDER BY priority, name",
            tuple(params),
        )
        return [WorkflowDefinitionRow.from_row(r) for r in rows]

    def import_from_yaml(
        self, yaml_content: str, project_id: str | None = None
    ) -> WorkflowDefinitionRow:
        """Import a workflow definition from YAML content."""
        import yaml

        data = yaml.safe_load(yaml_content)
        if not isinstance(data, dict) or "name" not in data:
            raise ValueError("Invalid workflow YAML: must be a dict with 'name' field")

        name = data["name"]
        description = data.get("description", "")
        _VALID_WORKFLOW_TYPES = {"rule", "variable", "agent", "pipeline"}
        yaml_type = data.get("type")
        if yaml_type not in _VALID_WORKFLOW_TYPES:
            raise ValueError(
                f"Invalid or missing 'type' in YAML: {yaml_type!r}. "
                f"Must be one of: {', '.join(sorted(_VALID_WORKFLOW_TYPES))}."
            )
        workflow_type = yaml_type
        version = str(data.get("version", "1.0"))
        enabled = bool(data.get("enabled", False))
        priority = data.get("priority", 100)
        sources_list = data.get("sources")

        return self.create(
            name=name,
            definition_json=json.dumps(data),
            workflow_type=workflow_type,
            project_id=project_id,
            description=description,
            version=version,
            enabled=enabled,
            priority=priority,
            sources=sources_list,
            source="installed",
        )

    def export_to_yaml(self, definition_id: str) -> str:
        """Export a workflow definition as YAML content."""
        import yaml

        row = self.get(definition_id)
        data = json.loads(row.definition_json)
        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def duplicate(self, definition_id: str, new_name: str) -> WorkflowDefinitionRow:
        """Duplicate a workflow definition with a new name."""
        original = self.get(definition_id)
        definition_data = json.loads(original.definition_json)
        definition_data["name"] = new_name

        return self.create(
            name=new_name,
            definition_json=json.dumps(definition_data),
            workflow_type=original.workflow_type,
            project_id=original.project_id,
            description=original.description,
            version=original.version,
            enabled=original.enabled,
            priority=original.priority,
            sources=original.sources,
            canvas_json=original.canvas_json,
            source="installed",
            tags=original.tags,
        )

    def move_to_project(self, definition_id: str, project_id: str) -> WorkflowDefinitionRow:
        """Move a definition to project scope."""
        return self.update(definition_id, source="project", project_id=project_id)

    def move_to_global(self, definition_id: str) -> WorkflowDefinitionRow:
        """Move a definition to global (installed) scope."""
        return self.update(definition_id, source="installed", project_id=None)
