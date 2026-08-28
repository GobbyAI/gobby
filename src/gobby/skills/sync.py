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

from gobby.skills.authoring import (
    find_bundled_content_violations,
    resolve_bundled_max_content_size,
)
from gobby.skills.loader import SkillLoader, SkillLoadError
from gobby.skills.parser import ParsedSkill

if TYPE_CHECKING:
    from gobby.skills._loader_models import LoadedSkillFile
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
    storage.create_skill_with_files(
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
        files=(
            _loaded_to_skill_files("", parsed.loaded_files)
            if parsed.loaded_files is not None
            else None
        ),
    )
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
    skill_files = (
        _loaded_to_skill_files(existing.id, parsed.loaded_files)
        if parsed.loaded_files is not None
        else None
    )
    existing_files = storage.get_skill_files(
        existing.id, include_content=False, exclude_license=False
    )
    existing_hashes = {item.path: item.content_hash for item in existing_files}
    incoming_hashes = (
        {item.path: item.content_hash for item in parsed.loaded_files}
        if parsed.loaded_files is not None
        else existing_hashes
    )

    needs_update = (
        existing.deleted_at is not None
        or existing.description != parsed.description
        or existing.content != parsed.content
        or existing.version != parsed.version
        or existing.license != parsed.license
        or existing.compatibility != parsed.compatibility
        or existing.allowed_tools != parsed.allowed_tools
        or existing.metadata != parsed.metadata
        or existing.always_apply != parsed.always_apply
        or existing.injection_format != parsed.injection_format
        or existing_hashes != incoming_hashes
    )

    if not needs_update:
        result["skipped"] += 1
        return

    restoring = existing.deleted_at is not None
    storage.update_skill_with_files(
        skill_id=existing.id,
        description=parsed.description,
        content=parsed.content,
        version=parsed.version,
        license=parsed.license,
        compatibility=parsed.compatibility,
        allowed_tools=parsed.allowed_tools,
        metadata=parsed.metadata,
        enabled=True if restoring else existing.enabled,
        always_apply=parsed.always_apply,
        injection_format=parsed.injection_format,
        clear_deleted_at=restoring,
        files=skill_files,
    )
    if restoring:
        logger.debug(
            "Restored soft-deleted bundled skill",
            extra={"skill_name": parsed.name, "path": str(parsed.source_path)},
        )
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
        "purged_project_overrides": 0,
        "errors": [],
        "warnings": [],
    }

    if not skills_path.exists():
        logger.warning("Bundled skills path not found", extra={"path": str(skills_path)})
        result["success"] = False
        result["errors"].append(f"Skills path not found: {skills_path}")
        return result
    if not skills_path.is_dir():
        logger.warning("Bundled skills path is not a directory", extra={"path": str(skills_path)})
        result["success"] = False
        result["errors"].append(f"Skills path is not a directory: {skills_path}")
        return result

    try:
        skill_directories = list(skills_path.iterdir())
    except OSError as e:
        error_msg = f"Failed to enumerate bundled skills path '{skills_path}': {e}"
        logger.error(
            "Failed to enumerate bundled skills path",
            extra={"path": str(skills_path), "error": str(e)},
        )
        result["success"] = False
        result["errors"].append(error_msg)
        return result

    limit = resolve_bundled_max_content_size(db)
    try:
        violations = find_bundled_content_violations(skills_path, limit)
    except OSError as e:
        error_msg = f"Failed to inspect bundled skills path '{skills_path}': {e}"
        logger.error(
            "Failed to inspect bundled skills path",
            extra={"path": str(skills_path), "error": str(e)},
        )
        result["success"] = False
        result["errors"].append(error_msg)
        return result
    for violation in violations:
        result["warnings"].append(violation.message)
        logger.warning(
            "Bundled skill instruction exceeds configured authoring ceiling",
            extra={
                "path": str(violation.path),
                "character_count": violation.character_count,
                "byte_count": violation.byte_count,
                "configured_limit": violation.limit,
                "guidance": violation.message,
            },
        )

    # Load skills using SkillLoader with 'filesystem' source type
    loader = SkillLoader(default_source_type="filesystem")
    storage = LocalSkillManager(db)

    parsed_skills: list[ParsedSkill] = []
    load_errors: list[str] = []
    for skill_dir in skill_directories:
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").exists():
            continue
        try:
            # validate=False for bundled skills since they're trusted and may have
            # version formats like "2.0" instead of strict semver "2.0.0"
            parsed_skills.append(loader.load_skill(skill_dir, validate=False))
        except (SkillLoadError, OSError, ValueError) as e:
            error_msg = f"Failed to load bundled skill '{skill_dir.name}': {e}"
            logger.error(
                "Failed to load bundled skill",
                extra={
                    "skill_name": skill_dir.name,
                    "path": str(skill_dir),
                    "error": str(e),
                },
            )
            load_errors.append(error_msg)

    if load_errors:
        result["success"] = False
        result["errors"].extend(load_errors)
    if not parsed_skills:
        result["success"] = False
        if not load_errors:
            result["errors"].append(f"No bundled skills found in: {skills_path}")

    # Track names on disk for orphan cleanup
    on_disk: set[str] = set()

    for parsed in parsed_skills:
        on_disk.add(parsed.name)
        try:
            _sync_single_skill(storage, parsed, result)
        except Exception as e:
            error_msg = f"Failed to sync skill '{parsed.name}': {e}"
            logger.error(
                "Failed to sync bundled skill",
                extra={
                    "skill_name": parsed.name,
                    "path": str(parsed.source_path),
                    "error": str(e),
                },
            )
            result["errors"].append(error_msg)

    # Orphan cleanup: soft-delete gobby-owned installed skills whose
    # SKILL.md was removed from disk
    if parsed_skills and not load_errors:
        all_installed = storage.list_skills(project_id=None, include_global=False, limit=-1)
        for skill in all_installed:
            if _is_gobby_owned(skill) and skill.name not in on_disk:
                storage.delete_skill(skill.id)
                logger.debug(
                    "Soft-deleted orphaned bundled skill",
                    extra={"skill_name": skill.name, "path": str(skill.source_path)},
                )
                result["orphaned"] += 1

    # Heal project-scoped rows sourced from bundled template trees: they
    # shadow the installed rows synced above with stale template content
    # (#17606). Creation is blocked in storage; this purges pre-existing rows.
    try:
        purged = storage.purge_bundled_template_project_skills()
        result["purged_project_overrides"] = len(purged)
    except Exception as e:
        error_msg = f"Failed to purge bundled template project skills: {e}"
        logger.error(
            "Failed to purge bundled template project skills",
            extra={"error": str(e)},
        )
        result["success"] = False
        result["errors"].append(error_msg)

    total = result["synced"] + result["updated"] + result["skipped"]
    logger.debug(
        "Bundled skill sync complete",
        extra={
            "synced": result["synced"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "orphaned": result["orphaned"],
            "purged_project_overrides": result["purged_project_overrides"],
            "total": total,
        },
    )

    return result
