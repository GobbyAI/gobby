"""Skill data models."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

SkillSourceType = Literal["local", "github", "url", "zip", "filesystem", "hub"]


@dataclass
class Skill:
    """A skill following the Agent Skills specification.

    Skills provide structured instructions for AI agents to follow when
    performing specific tasks. The format follows the Agent Skills spec
    (agentskills.io) with additional Gobby-specific extensions.

    Required fields per spec:
        - id: Unique identifier (prefixed with 'skl-')
        - name: Skill name (max 64 chars, lowercase+hyphens)
        - description: What the skill does (max 1024 chars)
        - content: The markdown body with instructions

    Optional spec fields:
        - version: Semantic version string
        - license: License identifier (e.g., "MIT")
        - compatibility: Compatibility notes (max 500 chars)
        - allowed_tools: List of allowed tool patterns
        - metadata: Free-form extension data (includes skillport/gobby namespaces)

    Source tracking:
        - source_path: Original file path or URL
        - source_type: 'local', 'github', 'url', 'zip', 'filesystem'
        - source_ref: Git ref for updates (branch/tag/commit)

    Hub Tracking:
        - hub_name: Name of the hub the skill originated from
        - hub_slug: Slug of the hub the skill originated from
        - hub_version: Version of the skill as reported by the hub

    Gobby-specific:
        - enabled: Toggle skill on/off without removing
        - project_id: NULL for global, else project-scoped
        - source: 'template' or 'installed' (template pattern)
        - deleted_at: Soft delete timestamp

    Timestamps:
        - created_at: ISO format creation timestamp
        - updated_at: ISO format last update timestamp
    """

    # Identity
    id: str
    name: str

    # Agent Skills Spec Fields
    description: str
    content: str
    version: str | None = None
    license: str | None = None
    compatibility: str | None = None
    allowed_tools: list[str] | None = None
    metadata: dict[str, Any] | None = None

    # Source Tracking
    source_path: str | None = None
    source_type: SkillSourceType | None = None
    source_ref: str | None = None

    # Hub Tracking
    hub_name: str | None = None
    hub_slug: str | None = None
    hub_version: str | None = None

    # Gobby-specific
    enabled: bool = True
    always_apply: bool = False
    injection_format: str = "summary"  # "summary", "full", "content"
    project_id: str | None = None

    # Template pattern (mirrors workflow_definitions)
    source: str = "installed"  # 'template' or 'installed'
    deleted_at: str | None = None

    # Timestamps
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Skill":
        """Create a Skill from a database row.

        Args:
            row: SQLite row with skill data

        Returns:
            Skill instance populated from the row
        """
        # Parse JSON fields
        allowed_tools_json = row["allowed_tools"]
        try:
            allowed_tools = json.loads(allowed_tools_json) if allowed_tools_json else None
        except json.JSONDecodeError:
            allowed_tools = None

        metadata_json = row["metadata"]
        try:
            metadata = json.loads(metadata_json) if metadata_json else None
        except json.JSONDecodeError:
            metadata = None

        row_keys = set(row.keys())

        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            content=row["content"],
            version=row["version"],
            license=row["license"],
            compatibility=row["compatibility"],
            allowed_tools=allowed_tools,
            metadata=metadata,
            source_path=row["source_path"],
            source_type=row["source_type"],
            source_ref=row["source_ref"],
            hub_name=row["hub_name"] if "hub_name" in row_keys else None,
            hub_slug=row["hub_slug"] if "hub_slug" in row_keys else None,
            hub_version=row["hub_version"] if "hub_version" in row_keys else None,
            enabled=bool(row["enabled"]),
            always_apply=bool(row["always_apply"]) if "always_apply" in row_keys else False,
            injection_format=row["injection_format"]
            if "injection_format" in row_keys
            else "summary",
            project_id=row["project_id"],
            source=row["source"] if "source" in row_keys else "installed",
            deleted_at=row["deleted_at"] if "deleted_at" in row_keys else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert skill to a dictionary representation.

        Returns:
            Dictionary with all skill fields
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "version": self.version,
            "license": self.license,
            "compatibility": self.compatibility,
            "allowed_tools": self.allowed_tools,
            "metadata": self.metadata,
            "source_path": self.source_path,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "hub_name": self.hub_name,
            "hub_slug": self.hub_slug,
            "hub_version": self.hub_version,
            "enabled": self.enabled,
            "always_apply": self.always_apply,
            "injection_format": self.injection_format,
            "project_id": self.project_id,
            "source": self.source,
            "deleted_at": self.deleted_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def get_category(self) -> str | None:
        """Get the skill category from top-level or metadata.skillport.category.

        Supports both top-level category and nested metadata.skillport.category.
        Top-level takes precedence.
        """
        if not self.metadata:
            return None
        # Check top-level first
        result = self.metadata.get("category")
        if result is not None:
            return str(result)
        # Fall back to nested skillport.category
        skillport = self.metadata.get("skillport", {})
        result = skillport.get("category")
        return str(result) if result is not None else None

    def get_tags(self) -> list[str]:
        """Get the skill tags from metadata.skillport.tags."""
        if not self.metadata:
            return []
        skillport = self.metadata.get("skillport", {})
        tags = skillport.get("tags", [])
        return list(tags) if isinstance(tags, list) else []

    def is_always_apply(self) -> bool:
        """Check if this is a core skill that should always be applied.

        Reads from the always_apply column first (set during sync from frontmatter).
        Falls back to metadata for backwards compatibility with older records.
        """
        # Primary: read from column (set during sync)
        if self.always_apply:
            return True
        # Fallback: check metadata for backwards compatibility
        if not self.metadata:
            return False
        # Check top-level first
        top_level = self.metadata.get("alwaysApply")
        if top_level is not None:
            return bool(top_level)
        # Fall back to nested skillport.alwaysApply
        skillport = self.metadata.get("skillport", {})
        return bool(skillport.get("alwaysApply", False))


# Change event types
ChangeEventType = Literal["create", "update", "delete"]


@dataclass
class ChangeEvent:
    """A change event fired when a skill is created, updated, or deleted.

    This event is passed to registered listeners when mutations occur,
    allowing components like search indexes to stay synchronized.

    Attributes:
        event_type: Type of change ('create', 'update', 'delete')
        skill_id: ID of the affected skill
        skill_name: Name of the affected skill (for logging/indexing)
        timestamp: ISO format timestamp of the event
        metadata: Optional additional context about the change
    """

    event_type: ChangeEventType
    skill_id: str
    skill_name: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary representation."""
        return {
            "event_type": self.event_type,
            "skill_id": self.skill_id,
            "skill_name": self.skill_name,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }


# Type alias for change listeners
ChangeListener = Any  # Callable[[ChangeEvent], None], but avoiding import issues
