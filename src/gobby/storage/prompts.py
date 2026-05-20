"""Prompt storage and management.

This module provides the PromptRecord dataclass and LocalPromptManager for
storing and retrieving prompts from SQLite, with three-tier scope precedence
(project > global > bundled) and read-only enforcement for bundled prompts.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.prompts.models import PromptTemplate, VariableSpec
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.id import generate_prefixed_id

__all__ = [
    "LocalPromptManager",
    "PromptChangeEvent",
    "PromptChangeNotifier",
    "PromptRecord",
]

logger = logging.getLogger(__name__)

# Sentinel for distinguishing "not provided" from explicit None
_UNSET: Any = object()

PromptScope = Literal["bundled", "global", "project"]
ChangeEventType = Literal["create", "update", "delete"]


@dataclass
class PromptRecord:
    """A prompt record stored in the database.

    Attributes:
        id: Unique identifier (prefixed with 'pmt-')
        name: Path-style name (e.g., "expansion/system")
        description: Human-readable description
        content: Raw template body (Jinja2 syntax)
        version: Template version string
        variables: JSON dict of variable specifications
        scope: 'bundled' | 'global' | 'project'
        source_path: Original file path (for bundled prompts)
        project_id: Project scope (None for bundled/global)
        enabled: Whether prompt is active
        created_at: ISO format creation timestamp
        updated_at: ISO format last update timestamp
    """

    id: str
    name: str
    description: str = ""
    content: str = ""
    version: str = "1.0"
    variables: dict[str, Any] | None = None
    scope: PromptScope = "bundled"
    source_path: str | None = None
    project_id: str | None = None
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "PromptRecord":
        """Create a PromptRecord from a database row."""
        variables_json = row["variables"]
        variables = json.loads(variables_json) if variables_json else None

        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            content=row["content"],
            version=row["version"] or "1.0",
            variables=variables,
            scope=row["scope"],
            source_path=row["source_path"],
            project_id=row["project_id"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "version": self.version,
            "variables": self.variables,
            "scope": self.scope,
            "source_path": self.source_path,
            "project_id": self.project_id,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_prompt_template(self) -> PromptTemplate:
        """Convert back to a PromptTemplate for backward compat with rendering.

        Returns:
            PromptTemplate instance with variables parsed from JSON.
        """
        variables: dict[str, VariableSpec] = {}
        if self.variables:
            for var_name, var_spec in self.variables.items():
                if isinstance(var_spec, dict):
                    variables[var_name] = VariableSpec(
                        type=var_spec.get("type", "str"),
                        default=var_spec.get("default"),
                        description=var_spec.get("description", ""),
                        required=var_spec.get("required", False),
                    )
                else:
                    variables[var_name] = VariableSpec(default=var_spec)

        from pathlib import Path

        return PromptTemplate(
            name=self.name,
            description=self.description,
            variables=variables,
            content=self.content,
            source_path=Path(self.source_path) if self.source_path else None,
            version=self.version,
        )


@dataclass
class PromptChangeEvent:
    """A change event fired when a prompt is created, updated, or deleted."""

    event_type: ChangeEventType
    prompt_id: str
    prompt_name: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary representation."""
        return {
            "event_type": self.event_type,
            "prompt_id": self.prompt_id,
            "prompt_name": self.prompt_name,
            "timestamp": self.timestamp,
        }


# Type alias for change listeners
ChangeListener = Any  # Callable[[PromptChangeEvent], None]


class PromptChangeNotifier:
    """Notifies registered listeners when prompts are mutated.

    Follows the same pattern as SkillChangeNotifier in storage/skills.py.
    """

    def __init__(self) -> None:
        self._listeners: list[ChangeListener] = []

    def add_listener(self, listener: ChangeListener) -> None:
        """Register a listener to receive change events."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: ChangeListener) -> bool:
        """Unregister a listener. Returns True if removed."""
        try:
            self._listeners.remove(listener)
            return True
        except ValueError:
            return False

    def fire_change(
        self,
        event_type: ChangeEventType,
        prompt_id: str,
        prompt_name: str,
    ) -> None:
        """Fire a change event to all registered listeners."""
        event = PromptChangeEvent(
            event_type=event_type,
            prompt_id=prompt_id,
            prompt_name=prompt_name,
        )
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as e:
                logger.error(
                    f"Error in prompt change listener {listener}: {e}",
                    exc_info=True,
                )

    def clear_listeners(self) -> None:
        """Remove all registered listeners."""
        self._listeners.clear()

    @property
    def listener_count(self) -> int:
        return len(self._listeners)


class LocalPromptManager:
    """Manages prompt storage in SQLite.

    Provides CRUD operations with three-tier scope precedence
    (project > global > bundled) and read-only enforcement for bundled prompts.
    """

    def __init__(
        self,
        db: HubDatabase,
        dev_mode: bool = False,
        notifier: PromptChangeNotifier | None = None,
    ) -> None:
        self.db = db
        self._dev_mode = dev_mode
        self._notifier = notifier

    def _notify_change(
        self,
        event_type: ChangeEventType,
        prompt_id: str,
        prompt_name: str,
    ) -> None:
        """Fire a change event if a notifier is configured."""
        if self._notifier is not None:
            try:
                self._notifier.fire_change(
                    event_type=event_type,
                    prompt_id=prompt_id,
                    prompt_name=prompt_name,
                )
            except Exception as e:
                logger.error(f"Error in prompt change notifier: {e}")

    def _check_bundled_writable(self, scope: str) -> None:
        """Raise ValueError if trying to mutate a bundled record in non-dev mode."""
        if scope == "bundled" and not self._dev_mode:
            raise ValueError(
                "Cannot modify bundled prompts outside dev mode. "
                "Create a scope='global' override instead."
            )

    def create_prompt(
        self,
        name: str,
        content: str,
        description: str = "",
        version: str = "1.0",
        variables: dict[str, Any] | None = None,
        scope: PromptScope = "bundled",
        source_path: str | None = None,
        project_id: str | None = None,
        enabled: bool = True,
    ) -> PromptRecord:
        """Create a new prompt record.

        Args:
            name: Path-style prompt name (e.g., "expansion/system")
            description: Human-readable description
            content: Raw template body
            version: Template version
            variables: Variable specifications dict
            scope: 'bundled', 'global', or 'project'
            source_path: Original file path
            project_id: Project scope (None for bundled/global)
            enabled: Whether prompt is active

        Returns:
            The created PromptRecord

        Raises:
            ValueError: If prompt already exists in that scope
        """
        now = datetime.now(UTC).isoformat()
        prompt_id = generate_prefixed_id("pmt", f"{name}:{scope}:{project_id or 'none'}")

        variables_json = json.dumps(variables) if variables else None

        with self.db.transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO prompts (
                        id, name, description, content, version, variables,
                        scope, source_path, project_id, enabled,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        prompt_id,
                        name,
                        description,
                        content,
                        version,
                        variables_json,
                        scope,
                        source_path,
                        project_id,
                        enabled,
                        now,
                        now,
                    ),
                )
            except Exception as e:
                if "UNIQUE constraint" in str(e):
                    raise ValueError(
                        f"Prompt '{name}' already exists with scope='{scope}'"
                        + (f" in project {project_id}" if project_id else "")
                    ) from e
                raise

        record = self.get_prompt(prompt_id)
        if record is None:
            raise RuntimeError(f"Failed to retrieve newly created prompt {prompt_id}")
        self._notify_change("create", prompt_id, name)
        return record

    def get_prompt(self, prompt_id: str) -> PromptRecord | None:
        """Get a prompt by ID.

        Returns:
            PromptRecord or None if not found
        """
        row = self.db.fetchone("SELECT * FROM prompts WHERE id = ?", (prompt_id,))
        if not row:
            return None
        return PromptRecord.from_row(row)

    def get_by_name(
        self,
        name: str,
        project_id: str | None = None,
    ) -> PromptRecord | None:
        """Get a prompt by name with precedence: project > global > bundled.

        Args:
            name: Prompt name (e.g., "expansion/system")
            project_id: Optional project context for project-scoped lookups

        Returns:
            Highest-priority PromptRecord, or None if not found
        """
        if project_id:
            row = self.db.fetchone(
                """
                SELECT * FROM prompts
                WHERE name = ? AND (project_id = ? OR project_id IS NULL)
                ORDER BY CASE scope
                    WHEN 'project' THEN 1
                    WHEN 'global' THEN 2
                    WHEN 'bundled' THEN 3
                END
                LIMIT 1
                """,
                (name, project_id),
            )
        else:
            row = self.db.fetchone(
                """
                SELECT * FROM prompts
                WHERE name = ? AND project_id IS NULL
                ORDER BY CASE scope
                    WHEN 'global' THEN 1
                    WHEN 'bundled' THEN 2
                END
                LIMIT 1
                """,
                (name,),
            )
        return PromptRecord.from_row(row) if row else None

    def get_bundled(self, name: str) -> PromptRecord | None:
        """Get the bundled version of a prompt (ignoring overrides).

        Useful for comparison/revert in the UI.
        """
        row = self.db.fetchone(
            "SELECT * FROM prompts WHERE name = ? AND scope = 'bundled'",
            (name,),
        )
        return PromptRecord.from_row(row) if row else None

    def update_prompt(
        self,
        prompt_id: str,
        name: str | None = None,
        description: str | None = None,
        content: str | None = None,
        version: str | None = _UNSET,
        variables: dict[str, Any] | None = _UNSET,
        source_path: str | None = _UNSET,
        enabled: bool | None = None,
    ) -> PromptRecord:
        """Update an existing prompt.

        Raises:
            ValueError: If prompt not found or if bundled and not in dev mode.
        """
        # Check scope before updating
        existing = self.get_prompt(prompt_id)
        if existing is None:
            raise ValueError(f"Prompt {prompt_id} not found")
        self._check_bundled_writable(existing.scope)

        updates = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if content is not None:
            updates.append("content = ?")
            params.append(content)
        if version is not _UNSET:
            updates.append("version = ?")
            params.append(version)
        if variables is not _UNSET:
            updates.append("variables = ?")
            params.append(json.dumps(variables) if variables else None)
        if source_path is not _UNSET:
            updates.append("source_path = ?")
            params.append(source_path)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(enabled)

        if not updates:
            return existing

        updates.append("updated_at = ?")
        params.append(datetime.now(UTC).isoformat())
        params.append(prompt_id)

        sql = f"UPDATE prompts SET {', '.join(updates)} WHERE id = ?"  # nosec B608

        with self.db.transaction() as conn:
            cursor = conn.execute(sql, tuple(params))
            if cursor.rowcount == 0:
                raise ValueError(f"Prompt {prompt_id} not found")

        record = self.get_prompt(prompt_id)
        if record is None:
            raise RuntimeError(f"Failed to retrieve updated prompt {prompt_id}")
        self._notify_change("update", prompt_id, record.name)
        return record

    def delete_prompt(self, prompt_id: str) -> bool:
        """Delete a prompt by ID.

        Raises:
            ValueError: If bundled and not in dev mode.
        """
        record = self.get_prompt(prompt_id)
        if record is None:
            return False

        self._check_bundled_writable(record.scope)

        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM prompts WHERE id = ?", (prompt_id,))
            if cursor.rowcount == 0:
                return False

        self._notify_change("delete", prompt_id, record.name)
        return True

    def list_prompts(
        self,
        project_id: str | None = None,
        scope: PromptScope | None = None,
        category: str | None = None,
        enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[PromptRecord]:
        """List prompts with optional filtering.

        Args:
            project_id: Filter by project (includes global/bundled)
            scope: Filter by scope
            category: Filter by category prefix (e.g., "expansion")
            enabled: Filter by enabled state
            limit: Maximum results
            offset: Results to skip

        Returns:
            List of matching PromptRecords
        """
        query = "SELECT * FROM prompts WHERE 1=1"
        params: list[Any] = []

        if project_id:
            query += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)
        elif scope != "project":
            # When no project_id and not explicitly asking for project scope,
            # only return non-project-scoped prompts
            query += " AND project_id IS NULL"

        if scope is not None:
            query += " AND scope = ?"
            params.append(scope)

        if category is not None:
            query += " AND name LIKE ?"
            params.append(f"{category}/%")

        if enabled is not None:
            query += " AND enabled = ?"
            params.append(enabled)

        query += " ORDER BY name ASC, scope ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [PromptRecord.from_row(row) for row in rows]

    def list_overrides(
        self,
        project_id: str | None = None,
    ) -> list[PromptRecord]:
        """Return scope='global' and scope='project' records (for export).

        Args:
            project_id: Optional project filter

        Returns:
            List of override PromptRecords
        """
        query = "SELECT * FROM prompts WHERE scope IN ('global', 'project')"
        params: list[Any] = []

        if project_id:
            query += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)

        query += " ORDER BY name ASC"
        rows = self.db.fetchall(query, tuple(params))
        return [PromptRecord.from_row(row) for row in rows]

    def search_prompts(
        self,
        query_text: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[PromptRecord]:
        """Search prompts by name and description."""
        escaped = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = """
            SELECT * FROM prompts
            WHERE (name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')
        """
        params: list[Any] = [f"%{escaped}%", f"%{escaped}%"]

        if project_id:
            sql += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)

        sql += " ORDER BY name ASC LIMIT ?"
        params.append(limit)

        rows = self.db.fetchall(sql, tuple(params))
        return [PromptRecord.from_row(row) for row in rows]

    def count_prompts(
        self,
        project_id: str | None = None,
        scope: PromptScope | None = None,
    ) -> int:
        """Count prompts matching criteria."""
        query = "SELECT COUNT(*) as count FROM prompts WHERE 1=1"
        params: list[Any] = []

        if project_id:
            query += " AND (project_id = ? OR project_id IS NULL)"
            params.append(project_id)

        if scope is not None:
            query += " AND scope = ?"
            params.append(scope)

        row = self.db.fetchone(query, tuple(params))
        return row["count"] if row else 0
