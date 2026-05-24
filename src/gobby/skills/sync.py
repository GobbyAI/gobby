"""Skill synchronization for bundled skills.

This module provides sync_bundled_skills() which loads skills from the
bundled install/shared/skills/ directory and syncs them to the database
as installed rows, following the same pattern as sync_bundled_rules().

Bundled skills are created with source='installed', enabled=True and
identified by metadata containing a 'gobby' key. On subsequent syncs,
gobby-tagged skills are overwritten from templates; user skills are
never touched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import ParsedSkill

if TYPE_CHECKING:
    from gobby.skills.loader import LoadedSkillFile
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.skills import LocalSkillManager, Skill, SkillFile

__all__: list[str] = ["sync_bundled_skills", "get_bundled_skills_path"]

logger = logging.getLogger(__name__)


def _loaded_to_skill_files(
    skill_id: str, loaded_files: list[LoadedSkillFile] | None
) -> list[SkillFile]:
    """Convert LoadedSkillFile list from loader to SkillFile list for storage."""
    if not loaded_files:
        return []
    return [
        SkillFile(
            id="",  # set_skill_files generates IDs for new files
            skill_id=skill_id,
            path=lf.path,
            file_type=lf.file_type,
            content=lf.content,
            content_hash=lf.content_hash,
            size_bytes=lf.size_bytes,
        )
        for lf in loaded_files
    ]


def _persist_skill_files(
    storage: LocalSkillManager,
    skill_id: str,
    loaded_files: list[LoadedSkillFile] | None,
) -> None:
    """Convert loaded files and persist to storage."""
    skill_files = _loaded_to_skill_files(skill_id, loaded_files)
    if skill_files:
        storage.set_skill_files(skill_id, skill_files)


def get_bundled_skills_path() -> Path:
    """Get the path to bundled skills directory.

    Returns:
        Path to src/gobby/install/shared/skills/
    """
    from gobby.paths import get_install_dir

    return get_install_dir() / "shared" / "skills"


def _is_gobby_owned(skill: Skill) -> bool:
    """Check if a skill is owned by gobby (bundled).

    Gobby-owned skills have a 'gobby' key in their metadata dict.
    """
    return bool(skill.metadata and "gobby" in skill.metadata)


def _sync_single_skill(
    storage: LocalSkillManager,
    parsed: ParsedSkill,
    result: dict[str, Any],
) -> None:
    """Sync a single parsed skill to the database as an installed row.

    - Row doesn't exist → create with source='installed', enabled=True
    - Gobby-tagged row exists → overwrite content from template (we own it)
    - Non-gobby row with same name exists → skip (user's skill)
    - Soft-deleted gobby row → restore and overwrite
    """
    existing = storage.get_by_name(parsed.name, project_id=None, include_deleted=True)

    if existing is not None:
        if _is_gobby_owned(existing):
            _handle_existing_gobby_skill(storage, existing, parsed, result)
        else:
            # User-created skill with same name — don't touch it
            result["skipped"] += 1
        return

    # No existing skill — create new installed row
    new_skill = storage.create_skill(
        name=parsed.name,
        description=parsed.description,
        content=parsed.content,
        version=parsed.version,
        license=parsed.license,
        compatibility=parsed.compatibility,
        allowed_tools=parsed.allowed_tools,
        metadata=parsed.metadata,
        source_path=parsed.source_path,
        source_type="filesystem",
        source_ref=None,
        project_id=None,
        enabled=True,
        always_apply=parsed.always_apply,
        injection_format=parsed.injection_format,
        source="installed",
    )
    _persist_skill_files(storage, new_skill.id, parsed.loaded_files)
    result["synced"] += 1


def _handle_existing_gobby_skill(
    storage: LocalSkillManager,
    existing: Skill,
    parsed: ParsedSkill,
    result: dict[str, Any],
) -> None:
    """Handle case where a gobby-owned installed row already exists.

    Restores soft-deleted skills and overwrites content from template.
    Preserves the user's enabled toggle.
    """
    # Restore soft-deleted skills
    if existing.deleted_at is not None:
        storage.restore(existing.id)
        storage.update_skill(
            skill_id=existing.id,
            description=parsed.description,
            content=parsed.content,
            version=parsed.version,
            license=parsed.license,
            compatibility=parsed.compatibility,
            allowed_tools=parsed.allowed_tools,
            metadata=parsed.metadata,
            enabled=True,
            always_apply=parsed.always_apply,
            injection_format=parsed.injection_format,
        )
        logger.info(f"Restored soft-deleted bundled skill: {parsed.name}")
        _persist_skill_files(storage, existing.id, parsed.loaded_files)
        result["updated"] += 1
        return

    # Check if content changed
    needs_update = (
        existing.description != parsed.description
        or existing.content != parsed.content
        or existing.version != parsed.version
        or existing.license != parsed.license
        or existing.compatibility != parsed.compatibility
        or existing.allowed_tools != parsed.allowed_tools
        or existing.metadata != parsed.metadata
        or existing.always_apply != parsed.always_apply
        or existing.injection_format != parsed.injection_format
    )

    if not needs_update:
        result["skipped"] += 1
        return

    # Overwrite from template, preserving user's enabled toggle
    storage.update_skill(
        skill_id=existing.id,
        description=parsed.description,
        content=parsed.content,
        version=parsed.version,
        license=parsed.license,
        compatibility=parsed.compatibility,
        allowed_tools=parsed.allowed_tools,
        metadata=parsed.metadata,
        enabled=existing.enabled,
        always_apply=parsed.always_apply,
        injection_format=parsed.injection_format,
    )
    _persist_skill_files(storage, existing.id, parsed.loaded_files)
    result["updated"] += 1


def sync_bundled_skills(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled skills from install/shared/skills/ to the database.

    Creates/updates skills as source='installed', enabled=True with
    gobby metadata. Gobby-owned skills (identified by metadata.gobby)
    are overwritten on sync. User skills are never touched.

    Args:
        db: Database connection

    Returns:
        Dict with success status and counts
    """
    skills_path = get_bundled_skills_path()

    result: dict[str, Any] = {
        "success": True,
        "synced": 0,
        "updated": 0,
        "skipped": 0,
        "orphaned": 0,
        "errors": [],
    }

    if not skills_path.exists():
        logger.warning(f"Bundled skills path not found: {skills_path}")
        result["errors"].append(f"Skills path not found: {skills_path}")
        return result

    # Load skills using SkillLoader with 'filesystem' source type
    loader = SkillLoader(default_source_type="filesystem")
    storage = LocalSkillManager(db)

    try:
        # validate=False for bundled skills since they're trusted and may have
        # version formats like "2.0" instead of strict semver "2.0.0"
        parsed_skills = loader.load_directory(skills_path, validate=False)
    except Exception as e:
        logger.error(f"Failed to load bundled skills: {e}")
        result["success"] = False
        result["errors"].append(f"Failed to load skills: {e}")
        return result

    # Track names on disk for orphan cleanup
    on_disk: set[str] = set()

    for parsed in parsed_skills:
        on_disk.add(parsed.name)
        try:
            _sync_single_skill(storage, parsed, result)
        except Exception as e:
            error_msg = f"Failed to sync skill '{parsed.name}': {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

    # Orphan cleanup: soft-delete gobby-owned installed skills whose
    # SKILL.md was removed from disk
    all_installed = storage.list_skills(project_id=None, include_global=False, limit=-1)
    for skill in all_installed:
        if _is_gobby_owned(skill) and skill.name not in on_disk:
            storage.delete_skill(skill.id)
            logger.info(f"Soft-deleted orphaned bundled skill: {skill.name}")
            result["orphaned"] += 1

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.info(
        f"Skill sync complete: {result['synced']} synced, "
        f"{result['updated']} updated, {result['skipped']} skipped, "
        f"{result['orphaned']} orphaned, {total} total"
    )

    return result
