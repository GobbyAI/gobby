"""Skill storage and management — composed from focused modules.

LocalSkillManager combines metadata CRUD and file I/O via mixins.
All public methods are inherited; see individual modules for details:

- ``_metadata.py`` — create, get, list, update, delete, search, count
- ``_files.py`` — set_skill_files, get_skill_files, delete/restore files
- ``LocalSkillManager`` — atomic metadata and file updates
"""

import json
import logging
from typing import Any

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills._errors import SkillMetadataValidationError
from gobby.storage.skills._files import SkillFilesMixin
from gobby.storage.skills._metadata import SkillMetadataMixin
from gobby.storage.skills._models import Skill, SkillFile, SkillSourceType
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)


class LocalSkillManager(SkillMetadataMixin, SkillFilesMixin):
    """Manages skill storage in the hub database.

    Provides CRUD operations for skills with support for:
    - Project-scoped uniqueness (UNIQUE(name, project_id, source))
    - Soft deletes
    - Category and tag filtering
    - Change notifications for search reindexing
    """

    def __init__(
        self,
        db: HubDatabase,
        notifier: Any | None = None,  # SkillChangeNotifier, avoid circular import
    ):
        """Initialize the skill manager.

        Args:
            db: Database protocol implementation
            notifier: Optional change notifier for mutations
        """
        self.db = db
        self._notifier = notifier

    def update_skill_with_files(
        self,
        skill_id: str,
        *,
        description: str,
        content: str,
        version: str | None,
        license: str | None,
        compatibility: str | None,
        allowed_tools: list[str] | None,
        metadata: dict[str, Any] | None,
        files: list[SkillFile] | None,
        always_apply: bool | None = None,
        injection_format: str | None = None,
        enabled: bool | None = None,
        clear_deleted_at: bool = False,
    ) -> Skill:
        """Atomically replace updater-managed metadata and optional files."""
        from gobby.skills.parser import SkillParseError, validate_runtime_metadata

        try:
            validate_runtime_metadata(metadata)
        except SkillParseError as exc:
            raise SkillMetadataValidationError(str(exc)) from exc

        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE skills
                   SET description = %s, content = %s, version = %s, license = %s,
                       compatibility = %s, allowed_tools = %s, metadata = %s,
                       always_apply = COALESCE(%s, always_apply),
                       injection_format = COALESCE(%s, injection_format),
                       enabled = COALESCE(%s, enabled),
                       deleted_at = CASE WHEN %s THEN NULL ELSE deleted_at END,
                       updated_at = %s
                   WHERE id = %s""",
                (
                    description,
                    content,
                    version,
                    license,
                    compatibility,
                    json.dumps(allowed_tools) if allowed_tools else None,
                    json.dumps(metadata) if metadata else None,
                    always_apply,
                    injection_format,
                    enabled,
                    clear_deleted_at,
                    utc_now(),
                    skill_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Skill {skill_id} not found")
            if files is not None:
                self._set_skill_files(conn, skill_id, files)

        skill = self.get_skill(skill_id)
        self._notify_change("update", skill_id, skill.name)
        return skill

    def create_skill_with_files(
        self,
        *,
        name: str,
        description: str,
        content: str,
        version: str | None = None,
        license: str | None = None,
        compatibility: str | None = None,
        allowed_tools: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source_path: str | None = None,
        source_type: SkillSourceType | None = None,
        source_ref: str | None = None,
        hub_name: str | None = None,
        hub_slug: str | None = None,
        hub_version: str | None = None,
        enabled: bool = True,
        always_apply: bool = False,
        injection_format: str = "summary",
        project_id: str | None = None,
        source: str = "installed",
        files: list[SkillFile] | None,
    ) -> Skill:
        """Atomically publish a skill row and an optional loaded file inventory."""
        with self.db.transaction() as conn:
            skill_id = self._create_skill_in_transaction(
                conn,
                name=name,
                description=description,
                content=content,
                version=version,
                license=license,
                compatibility=compatibility,
                allowed_tools=allowed_tools,
                metadata=metadata,
                source_path=source_path,
                source_type=source_type,
                source_ref=source_ref,
                hub_name=hub_name,
                hub_slug=hub_slug,
                hub_version=hub_version,
                enabled=enabled,
                always_apply=always_apply,
                injection_format=injection_format,
                project_id=project_id,
                source=source,
            )
            if files is not None:
                self._set_skill_files(conn, skill_id, files)

        skill = self.get_skill(skill_id)
        self._notify_change("create", skill_id, name)
        return skill

    def _notify_change(
        self,
        event_type: str,
        skill_id: str,
        skill_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Fire a change event if a notifier is configured.

        Args:
            event_type: Type of change ('create', 'update', 'delete')
            skill_id: ID of the affected skill
            skill_name: Name of the affected skill
            metadata: Optional additional metadata
        """
        if self._notifier is not None:
            try:
                self._notifier.fire_change(
                    event_type=event_type,
                    skill_id=skill_id,
                    skill_name=skill_name,
                    metadata=metadata,
                )
            except Exception as e:
                logger.error("Error in skill change notifier: %s", e)
