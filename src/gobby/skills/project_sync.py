"""Startup sync of project-folder skills into project-scoped rows.

Each registered checkout's ``.gobby/skills/<name>/`` folder becomes a
project-scoped skill row for that project. Rows the sync writes carry the
``project_sync`` metadata marker. Only marked rows are updated or retired, so
operator installs and external-source rows in the same project stay put.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import ParsedSkill
from gobby.skills.sync import _is_gobby_owned, _loaded_to_skill_files
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import get_project_gobby_root
from gobby.storage.projects import CHECKOUT_FREE_PROJECT_IDS, LocalProjectManager
from gobby.storage.skills import LocalSkillManager, Skill

__all__ = ["PROJECT_SYNC_KEY", "sync_project_skills"]

PROJECT_SYNC_KEY = "project_sync"
_SYNC_MARKER: dict[str, str] = {"owner": "gobby"}
_PROJECT_SKILL_PAGE = 1000

logger = logging.getLogger(__name__)


def sync_project_skills(db: HubDatabase) -> dict[str, Any]:
    """Sync each registered checkout's project skill folders into project rows."""
    result: dict[str, Any] = {
        "synced": 0,
        "updated": 0,
        "orphaned": 0,
        "skipped": 0,
        "errors": [],
        "warnings": [],
    }
    storage = LocalSkillManager(db)
    loader = SkillLoader(default_source_type="filesystem")
    for project in LocalProjectManager(db).list():
        if project.id in CHECKOUT_FREE_PROJECT_IDS:
            continue
        gobby_root = get_project_gobby_root(db, project.id)
        # An unavailable checkout must never authorize retiring its skills.
        if gobby_root is None or not gobby_root.is_dir():
            continue
        _sync_project(storage, loader, project.id, gobby_root / "skills", result)
    return result


def _sync_project(
    storage: LocalSkillManager,
    loader: SkillLoader,
    project_id: str,
    skills_root: Path,
    result: dict[str, Any],
) -> None:
    on_disk: set[str] = set()
    scan_complete = True
    try:
        # An absent skills folder in a live checkout is an empty scan.
        folders = sorted(skills_root.iterdir()) if skills_root.is_dir() else []
    except OSError as exc:
        message = f"Failed to scan project skills {skills_root}: {exc}"
        logger.warning(message)
        result["errors"].append(message)
        return
    for folder in folders:
        if not folder.is_dir() or not (folder / "SKILL.md").is_file():
            continue
        try:
            parsed = loader.load_skill(folder)
            _sync_one(storage, project_id, parsed, result)
        except Exception as exc:
            scan_complete = False
            message = f"Failed to sync project skill {folder}: {exc}"
            logger.warning(message)
            result["errors"].append(message)
            continue
        on_disk.add(parsed.name)
    if scan_complete:
        _retire_missing(storage, project_id, on_disk, result)


def _sync_one(
    storage: LocalSkillManager,
    project_id: str,
    parsed: ParsedSkill,
    result: dict[str, Any],
) -> None:
    if _shadows_bundled(storage, parsed.name):
        message = f"Project skill '{parsed.name}' shadows a bundled skill; skipped"
        logger.warning(message)
        result["skipped"] += 1
        result["warnings"].append(message)
        return

    metadata = {**(parsed.metadata or {}), PROJECT_SYNC_KEY: dict(_SYNC_MARKER)}
    existing = _find_project_row(storage, parsed.name, project_id)
    if existing is not None and not _is_sync_owned(existing):
        message = f"Project skill '{parsed.name}' is held by a row the sync does not own; skipped"
        logger.warning(message)
        result["skipped"] += 1
        result["warnings"].append(message)
        return
    if existing is None:
        storage.create_skill_with_files(
            name=parsed.name,
            description=parsed.description,
            content=parsed.content,
            version=parsed.version,
            license=parsed.license,
            compatibility=parsed.compatibility,
            allowed_tools=parsed.allowed_tools,
            metadata=metadata,
            source_path=parsed.source_path,
            source_type="filesystem",
            project_id=project_id,
            enabled=True,
            always_apply=parsed.always_apply,
            injection_format=parsed.injection_format,
            source="project",
            files=(
                _loaded_to_skill_files("", parsed.loaded_files)
                if parsed.loaded_files is not None
                else None
            ),
        )
        result["synced"] += 1
        return

    if not _needs_update(storage, existing, parsed, metadata):
        result["skipped"] += 1
        return
    restoring = existing.deleted_at is not None
    storage.update_skill_with_files(
        existing.id,
        description=parsed.description,
        content=parsed.content,
        version=parsed.version,
        license=parsed.license,
        compatibility=parsed.compatibility,
        allowed_tools=parsed.allowed_tools,
        metadata=metadata,
        always_apply=parsed.always_apply,
        injection_format=parsed.injection_format,
        enabled=True if restoring else existing.enabled,
        clear_deleted_at=restoring,
        source_path=parsed.source_path,
        files=(
            _loaded_to_skill_files(existing.id, parsed.loaded_files)
            if parsed.loaded_files is not None
            else None
        ),
    )
    result["updated"] += 1


def _retire_missing(
    storage: LocalSkillManager,
    project_id: str,
    on_disk: set[str],
    result: dict[str, Any],
) -> None:
    rows = storage.list_skills(
        project_id=project_id, include_global=False, limit=_PROJECT_SKILL_PAGE
    )
    for row in rows:
        if row.name in on_disk or not _is_sync_owned(row):
            continue
        storage.delete_skill(row.id)
        result["orphaned"] += 1


def _shadows_bundled(storage: LocalSkillManager, name: str) -> bool:
    row = storage.get_by_name(name, project_id=None, include_global=False)
    return row is not None and _is_gobby_owned(row)


def _find_project_row(storage: LocalSkillManager, name: str, project_id: str) -> Skill | None:
    """Return the project's live row for ``name``, else its soft-deleted sync-owned row.

    A soft-deleted row the sync did not create is never adopted; the sync creates
    a new row beside it instead.
    """
    live = storage.get_by_name(name, project_id=project_id, include_global=False)
    if live is not None:
        return live
    deleted = storage.get_by_name(
        name, project_id=project_id, include_global=False, include_deleted=True
    )
    if deleted is not None and _is_sync_owned(deleted):
        return deleted
    return None


def _is_sync_owned(row: Skill) -> bool:
    return isinstance(row.metadata, dict) and PROJECT_SYNC_KEY in row.metadata


def _needs_update(
    storage: LocalSkillManager,
    existing: Skill,
    parsed: ParsedSkill,
    metadata: dict[str, Any],
) -> bool:
    if existing.deleted_at is not None or existing.source_path != parsed.source_path:
        return True
    unchanged = (
        existing.description,
        existing.content,
        existing.version,
        existing.license,
        existing.compatibility,
        existing.allowed_tools,
        existing.metadata,
        existing.always_apply,
        existing.injection_format,
    ) == (
        parsed.description,
        parsed.content,
        parsed.version,
        parsed.license,
        parsed.compatibility,
        parsed.allowed_tools,
        metadata,
        parsed.always_apply,
        parsed.injection_format,
    )
    if not unchanged:
        return True
    existing_files = storage.get_skill_files(
        existing.id, include_content=False, exclude_license=False
    )
    existing_hashes = {item.path: item.content_hash for item in existing_files}
    incoming_hashes = (
        {item.path: item.content_hash for item in parsed.loaded_files}
        if parsed.loaded_files is not None
        else existing_hashes
    )
    return existing_hashes != incoming_hashes
