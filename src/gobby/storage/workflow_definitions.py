"""Workflow definition storage manager for local database."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Row
from typing import Any
from uuid import uuid4

from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


@dataclass
class WorkflowDefinitionRow:
    """Represents a workflow definition row from the database."""

    id: str
    name: str
    workflow_type: str
    enabled: bool
    priority: int
    definition_json: str
    source: str
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
    def from_row(cls, row: Row) -> "WorkflowDefinitionRow":
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

    def __init__(self, db: DatabaseProtocol):
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
        source: str = "installed",
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
                    1 if enabled else 0,
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
        include_templates: bool = False,
    ) -> WorkflowDefinitionRow | None:
        """Get a workflow definition by name (project-scoped first, then global fallback)."""
        deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
        template_filter = "" if include_templates else " AND source != 'template'"
        if project_id:
            row = self.db.fetchone(
                f"SELECT * FROM workflow_definitions WHERE name = ? AND project_id = ?{deleted_filter}{template_filter}",
                (name, project_id),
            )
            if row:
                return WorkflowDefinitionRow.from_row(row)
        row = self.db.fetchone(
            f"SELECT * FROM workflow_definitions WHERE name = ? AND project_id IS NULL{deleted_filter}{template_filter}",
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
                values[key] = 1 if val else 0
            else:
                values[key] = val

        if not values:
            return self.get(definition_id)

        values["updated_at"] = datetime.now(UTC).isoformat()
        self.db.safe_update("workflow_definitions", values, "id = ?", (definition_id,))
        return self.get(definition_id)

    def delete(self, definition_id: str) -> bool:
        """Soft-delete a workflow definition by setting deleted_at."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE workflow_definitions SET deleted_at = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now, now, definition_id),
            )
            return cursor.rowcount > 0

    def hard_delete(self, definition_id: str) -> bool:
        """Permanently delete a workflow definition from the database."""
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM workflow_definitions WHERE id = ?", (definition_id,))
            return cursor.rowcount > 0

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
        return self.get(definition_id)

    def purge_deleted(self, older_than_days: int = 30) -> int:
        """Hard-delete rows that were soft-deleted more than older_than_days ago."""
        if older_than_days < 1:
            raise ValueError(f"older_than_days must be >= 1, got {older_than_days}")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM workflow_definitions WHERE deleted_at IS NOT NULL AND deleted_at < datetime('now', ?)",
                (f"-{older_than_days} days",),
            )
            count = cursor.rowcount
        if count:
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
            params.append(1 if enabled else 0)

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
        include_templates: bool = False,
    ) -> list[WorkflowDefinitionRow]:
        """List rule definitions filtered by event type from definition_json."""
        conditions = [
            "workflow_type = 'rule'",
            "deleted_at IS NULL",
            "json_extract(definition_json, '$.event') = ?",
        ]
        if not include_templates:
            conditions.append("source != 'template'")
        params: list[Any] = [event]

        if project_id:
            conditions.append("(project_id = ? OR project_id IS NULL)")
            params.append(project_id)

        if enabled is not None:
            conditions.append("enabled = ?")
            params.append(1 if enabled else 0)

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
        include_templates: bool = False,
    ) -> list[WorkflowDefinitionRow]:
        """List rule definitions filtered by group from definition_json."""
        conditions = [
            "workflow_type = 'rule'",
            "deleted_at IS NULL",
            "json_extract(definition_json, '$.group') = ?",
        ]
        if not include_templates:
            conditions.append("source != 'template'")
        params: list[Any] = [group]

        if project_id:
            conditions.append("(project_id = ? OR project_id IS NULL)")
            params.append(project_id)

        if enabled is not None:
            conditions.append("enabled = ?")
            params.append(1 if enabled else 0)

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
        yaml_type = data.get("type", "")
        workflow_type = "pipeline" if yaml_type == "pipeline" else "workflow"
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
        row = self.get(definition_id)
        if row.source == "template":
            raise ValueError("Cannot move a template definition")
        return self.update(definition_id, source="project", project_id=project_id)

    def move_to_global(self, definition_id: str) -> WorkflowDefinitionRow:
        """Move a definition to global (installed) scope."""
        row = self.get(definition_id)
        if row.source == "template":
            raise ValueError("Cannot move a template definition")
        return self.update(definition_id, source="installed", project_id=None)

    def install_from_template(self, definition_id: str) -> WorkflowDefinitionRow:
        """Create an installed copy from a template definition.

        Copies all fields with source='installed', preserving the template's enabled state.
        Disables the template original to prevent duplicate rule firing.
        """
        original = self.get(definition_id)
        if original.source != "template":
            raise ValueError(
                f"Definition '{original.name}' is not a template (source={original.source})"
            )

        # Check for existing installed item with same name
        existing = self.db.fetchone(
            "SELECT id FROM workflow_definitions WHERE name = ? AND source != 'template' AND deleted_at IS NULL",
            (original.name,),
        )
        if existing:
            raise ValueError(f"An installed definition named '{original.name}' already exists")

        # Create the installed copy
        new_row = self.create(
            name=original.name,
            definition_json=original.definition_json,
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

        # Disable the template original
        self.update(definition_id, enabled=False)

        return new_row

    def install_all_templates(
        self, workflow_type: str | None = None
    ) -> list[WorkflowDefinitionRow]:
        """Create installed copies of all eligible template definitions.

        Skips template items that already have an installed counterpart with the same name.
        """
        templates = self.list_all(workflow_type=workflow_type, include_deleted=False)
        templates = [row for row in templates if row.source == "template"]

        created: list[WorkflowDefinitionRow] = []
        for row in templates:
            # Skip if a non-template item with same name already exists
            existing = self.db.fetchone(
                "SELECT id FROM workflow_definitions WHERE name = ? AND source != 'template' AND deleted_at IS NULL",
                (row.name,),
            )
            if existing:
                continue
            try:
                new_row = self.install_from_template(row.id)
                created.append(new_row)
            except ValueError:
                continue

        return created
