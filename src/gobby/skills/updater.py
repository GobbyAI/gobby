"""SkillUpdater - Refresh skills from their source.

This module provides the SkillUpdater class for updating skills from:
- Local filesystem paths
- GitHub repositories
- ZIP archives (future)

Features:
- Backup before update with automatic rollback on failure
- Support for bulk update_all() operations
- Graceful handling of missing or invalid sources
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.skills.loader import (
    SkillLoader,
    SkillLoadError,
    clone_skill_repo,
    parse_github_url,
)
from gobby.skills.parser import ParsedSkill, SkillParseError, parse_skill_file
from gobby.skills.validator import SkillValidator

if TYPE_CHECKING:
    from gobby.storage.skills import LocalSkillManager, Skill

logger = logging.getLogger(__name__)


class SkillUpdateError(Exception):
    """Error updating a skill from source."""

    def __init__(self, message: str, skill_id: str | None = None):
        self.skill_id = skill_id
        super().__init__(message)


@dataclass
class SkillUpdateResult:
    """Result of a skill update operation.

    Attributes:
        skill_id: ID of the skill that was updated
        skill_name: Name of the skill
        success: Whether the update succeeded
        updated: Whether the skill content changed
        error: Error message if update failed
        backup_created: Whether a backup was created
        rolled_back: Whether changes were rolled back
        skipped: Whether update was skipped (no source)
        skip_reason: Reason for skipping
    """

    skill_id: str
    skill_name: str
    success: bool = True
    updated: bool = False
    error: str | None = None
    backup_created: bool = False
    rolled_back: bool = False
    skipped: bool = False
    skip_reason: str | None = None


@dataclass
class _SkillBackup:
    """Internal backup of skill data for rollback."""

    skill_id: str
    name: str
    description: str
    content: str
    version: str | None
    license: str | None
    compatibility: str | None
    allowed_tools: list[str] | None
    metadata: dict[str, Any] | None


class SkillUpdater:
    """Update skills from their source locations.

    This class handles:
    - Fetching latest version from source (local, GitHub)
    - Backing up current version before update
    - Rolling back on validation/parse failures
    - Bulk update of all sourceable skills

    Example usage:
        ```python
        from gobby.skills.updater import SkillUpdater
        from gobby.storage.skills import LocalSkillManager

        storage = LocalSkillManager(db)
        updater = SkillUpdater(storage)

        # Update a single skill
        result = updater.update_skill(skill_id)
        if result.success:
            print(f"Updated {result.skill_name}")

        # Update all skills with sources
        results = updater.update_all()
        for r in results:
            print(f"{r.skill_name}: {'updated' if r.updated else 'unchanged'}")
        ```
    """

    def __init__(
        self,
        storage: LocalSkillManager,
    ):
        """Initialize the updater.

        Args:
            storage: Storage manager for skill CRUD operations
        """
        self._storage = storage
        self._loader = SkillLoader()
        self._validator = SkillValidator()

    def update_skill(
        self,
        skill_id: str,
        cache_dir: Path | None = None,
    ) -> SkillUpdateResult:
        """Update a skill from its source.

        Args:
            skill_id: ID of the skill to update
            cache_dir: Optional cache directory for GitHub repos

        Returns:
            SkillUpdateResult with update status
        """
        # Get current skill
        try:
            skill = self._storage.get_skill(skill_id)
        except ValueError as e:
            return SkillUpdateResult(
                skill_id=skill_id,
                skill_name="unknown",
                success=False,
                error=f"Skill not found: {e}",
            )

        # Check if skill has a source
        if not skill.source_path or not skill.source_type:
            return SkillUpdateResult(
                skill_id=skill_id,
                skill_name=skill.name,
                success=True,
                updated=False,
                skipped=True,
                skip_reason="No source path or source type",
            )

        # Create backup
        backup = self._create_backup(skill)

        try:
            # Fetch updated content based on source type
            if skill.source_type == "github":
                parsed = self._fetch_from_github(skill, cache_dir)
            elif skill.source_type in ("local", "filesystem"):
                parsed = self._fetch_from_local(skill)
            else:
                return SkillUpdateResult(
                    skill_id=skill_id,
                    skill_name=skill.name,
                    success=True,
                    updated=False,
                    skipped=True,
                    skip_reason=f"Unknown source type: {skill.source_type}",
                )

            # Check if content changed
            if not self._has_changes(skill, parsed):
                return SkillUpdateResult(
                    skill_id=skill_id,
                    skill_name=skill.name,
                    success=True,
                    updated=False,
                    backup_created=True,
                )

            # Validate the updated skill
            validation = self._validator.validate(parsed)
            if not validation.valid:
                raise SkillUpdateError(
                    f"Validation failed: {'; '.join(validation.errors)}",
                    skill_id=skill_id,
                )

            # Rescan the refreshed content before persisting (TOCTOU guard):
            # a source that passed install-time scanning can swap in a
            # hostile payload on refresh. Unsafe updates are rejected and the
            # stored version is kept.
            self._scan_update(skill, parsed)

            # Apply update
            self._apply_update(skill, parsed)

            return SkillUpdateResult(
                skill_id=skill_id,
                skill_name=skill.name,
                success=True,
                updated=True,
                backup_created=True,
            )

        except (SkillLoadError, SkillParseError, SkillUpdateError) as e:
            # Rollback on failure
            rollback_succeeded = self._restore_backup(backup)
            return SkillUpdateResult(
                skill_id=skill_id,
                skill_name=skill.name,
                success=False,
                error=str(e),
                backup_created=True,
                rolled_back=rollback_succeeded,
            )
        except Exception as e:
            # Rollback on unexpected errors too
            rollback_succeeded = self._restore_backup(backup)
            logger.exception("Unexpected error updating skill %s", skill_id)
            return SkillUpdateResult(
                skill_id=skill_id,
                skill_name=skill.name,
                success=False,
                error=f"Unexpected error: {e}",
                backup_created=True,
                rolled_back=rollback_succeeded,
            )

    def update_all(
        self,
        cache_dir: Path | None = None,
    ) -> list[SkillUpdateResult]:
        """Update all skills that have sources.

        Args:
            cache_dir: Optional cache directory for GitHub repos

        Returns:
            List of SkillUpdateResult for each updated skill
        """
        results: list[SkillUpdateResult] = []

        # Get all skills
        skills = self._storage.list_skills(limit=10000)

        for skill in skills:
            # Skip skills without sources
            if not skill.source_path or not skill.source_type:
                continue

            result = self.update_skill(skill.id, cache_dir=cache_dir)
            results.append(result)

        return results

    def _create_backup(self, skill: Skill) -> _SkillBackup:
        """Create a backup of skill data for rollback."""
        return _SkillBackup(
            skill_id=skill.id,
            name=skill.name,
            description=skill.description,
            content=skill.content,
            version=skill.version,
            license=skill.license,
            compatibility=skill.compatibility,
            allowed_tools=skill.allowed_tools,
            metadata=skill.metadata,
        )

    def _restore_backup(self, backup: _SkillBackup) -> bool:
        """Restore skill from backup.

        Returns:
            True if restore succeeded, False if it failed
        """
        try:
            self._storage.update_skill(
                skill_id=backup.skill_id,
                name=backup.name,
                description=backup.description,
                content=backup.content,
                version=backup.version,
                license=backup.license,
                compatibility=backup.compatibility,
                allowed_tools=backup.allowed_tools,
                metadata=backup.metadata,
            )
            return True
        except Exception as e:
            logger.error("Failed to restore backup for skill %s: %s", backup.skill_id, e)
            return False

    def _fetch_from_local(self, skill: Skill) -> ParsedSkill:
        """Fetch updated skill from local filesystem."""
        if skill.source_path is None:
            raise SkillLoadError("Source path is not set")
        source_path = Path(skill.source_path)

        # Check if path exists
        if not source_path.exists():
            raise SkillLoadError("Source path not found", source_path)

        # Use loader for directory loads to capture all files
        if source_path.is_dir():
            return self._loader.load_skill(source_path, validate=False, check_dir_name=False)
        elif source_path.name == "SKILL.md" and source_path.parent.is_dir():
            # Source path points to SKILL.md — load the parent directory
            return self._loader.load_skill(source_path.parent, validate=False, check_dir_name=False)
        else:
            return parse_skill_file(source_path)

    def _fetch_from_github(
        self,
        skill: Skill,
        cache_dir: Path | None = None,
    ) -> ParsedSkill:
        """Fetch updated skill from GitHub."""
        # Parse the source path to get GitHub ref
        if skill.source_path is None:
            raise SkillLoadError("Source path is not set")
        source: str = skill.source_path
        if source.startswith("github:"):
            source = source[7:]  # Remove github: prefix

        # Add branch if stored
        if skill.source_ref:
            if "#" not in source:
                source = f"{source}#{skill.source_ref}"

        ref = parse_github_url(source)
        repo_path = clone_skill_repo(ref, cache_dir=cache_dir)

        # Determine skill path in repo
        if ref.path:
            skill_path = repo_path / ref.path
        else:
            skill_path = repo_path

        # Load the skill
        skill_file = skill_path / "SKILL.md"
        if not skill_file.exists():
            # Maybe it's a single-skill repo
            skill_file = skill_path
            if not skill_file.exists() or skill_file.is_dir():
                skill_file = skill_path / "SKILL.md"

        if skill_file.is_dir():
            # Use loader for directory loads to capture all files
            return self._loader.load_skill(skill_file, validate=False, check_dir_name=False)

        if not skill_file.exists():
            raise SkillLoadError("SKILL.md not found in repository", repo_path)

        # If pointing to a SKILL.md, load parent directory to get files
        if skill_file.name == "SKILL.md" and skill_file.parent.is_dir():
            return self._loader.load_skill(skill_file.parent, validate=False, check_dir_name=False)

        return parse_skill_file(skill_file)

    def _has_changes(self, skill: Skill, parsed: ParsedSkill) -> bool:
        """Check if the parsed skill differs from current."""
        if (
            skill.description != parsed.description
            or skill.content != parsed.content
            or skill.version != parsed.version
            or skill.license != parsed.license
            or skill.compatibility != parsed.compatibility
            or skill.allowed_tools != parsed.allowed_tools
            or skill.metadata != parsed.metadata
        ):
            return True

        # Check file hashes
        if parsed.loaded_files:
            try:
                existing_files = self._storage.get_skill_files(
                    skill.id, include_content=False, exclude_license=False
                )
                existing_hashes = {f.path: f.content_hash for f in existing_files}
                incoming_hashes = {lf.path: lf.content_hash for lf in parsed.loaded_files}
                if existing_hashes != incoming_hashes:
                    return True
            except Exception:
                # Table may not exist yet during migration
                return True

        return False

    def _scan_update(self, skill: Skill, parsed: ParsedSkill) -> None:
        """Security-scan refreshed content; raise SkillUpdateError if unsafe.

        External sources fail closed when the scanner is unavailable; local
        sources keep warn-and-continue.
        """
        from gobby.skills.scanner import is_external_source, scan_parsed_skill

        try:
            scan_result = scan_parsed_skill(parsed, name=skill.name)
        except ImportError:
            if is_external_source(skill.source_type):
                raise SkillUpdateError(
                    "clawcare is not installed; refusing to update skill from "
                    f"external source ({skill.source_type}) without a security scan",
                    skill_id=skill.id,
                ) from None
            logger.warning("clawcare not installed, skipping security scan for %s", skill.name)
            return

        if not scan_result["is_safe"]:
            findings_summary = "; ".join(
                f"[{f['severity']}] {f['title']} at {f['location']}"
                for f in scan_result["findings"]
                if f["severity"] in ("HIGH", "CRITICAL")
            )
            raise SkillUpdateError(
                f"Updated content failed security scan "
                f"(max severity: {scan_result['max_severity']}): {findings_summary}",
                skill_id=skill.id,
            )

    def _apply_update(self, skill: Skill, parsed: ParsedSkill) -> None:
        """Apply parsed skill data to storage."""
        skill_files = None
        if parsed.loaded_files:
            from gobby.storage.skills import SkillFile

            skill_files = [
                SkillFile(
                    id="",
                    skill_id=skill.id,
                    path=lf.path,
                    file_type=lf.file_type,
                    content=lf.content,
                    content_hash=lf.content_hash,
                    size_bytes=lf.size_bytes,
                )
                for lf in parsed.loaded_files
            ]

        self._storage.update_skill_with_files(
            skill_id=skill.id,
            description=parsed.description,
            content=parsed.content,
            version=parsed.version,
            license=parsed.license,
            compatibility=parsed.compatibility,
            allowed_tools=parsed.allowed_tools,
            metadata=parsed.metadata,
            files=skill_files,
        )
