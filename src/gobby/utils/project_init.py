"""
Shared project initialization utilities.

This module provides the core logic for initializing a Gobby project,
used by both the CLI and the hook system for auto-initialization.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


@dataclass
class VerificationCommands:
    """Auto-detected verification commands for a project."""

    unit_tests: str | None = None
    type_check: str | None = None
    lint: str | None = None
    format: str | None = None
    build: str | None = None
    doc_tests: str | None = None
    integration: str | None = None
    custom: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        result: dict[str, Any] = {}
        if self.unit_tests:
            result["unit_tests"] = self.unit_tests
        if self.type_check:
            result["type_check"] = self.type_check
        if self.lint:
            result["lint"] = self.lint
        if self.format:
            result["format"] = self.format
        if self.build:
            result["build"] = self.build
        if self.doc_tests:
            result["doc_tests"] = self.doc_tests
        if self.integration:
            result["integration"] = self.integration
        if self.custom:
            result["custom"] = self.custom
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationCommands:
        """Create commands from project verification data."""
        custom = data.get("custom", {})
        return cls(
            unit_tests=data.get("unit_tests") if isinstance(data.get("unit_tests"), str) else None,
            type_check=data.get("type_check") if isinstance(data.get("type_check"), str) else None,
            lint=data.get("lint") if isinstance(data.get("lint"), str) else None,
            format=data.get("format") if isinstance(data.get("format"), str) else None,
            build=data.get("build") if isinstance(data.get("build"), str) else None,
            doc_tests=data.get("doc_tests") if isinstance(data.get("doc_tests"), str) else None,
            integration=data.get("integration")
            if isinstance(data.get("integration"), str)
            else None,
            custom={str(k): str(v) for k, v in custom.items()} if isinstance(custom, dict) else {},
        )


@dataclass
class InitResult:
    """Result of project initialization."""

    project_id: str
    project_name: str
    project_path: str
    created_at: str
    already_existed: bool
    verification: VerificationCommands | None = None


_FRONTEND_SUBDIRS = ["web", "frontend", "client", "app", "ui", "packages/web", "packages/frontend"]


def _find_frontend_dirs(cwd: Path) -> list[tuple[Path, str]]:
    """Find directories containing package.json (root + common frontend subdirs).

    Returns list of (absolute_path, relative_name) tuples. "." for root.
    """
    results: list[tuple[Path, str]] = []

    # Check root first
    if (cwd / "package.json").exists():
        results.append((cwd, "."))

    # Check common frontend subdirectories
    for subdir in _FRONTEND_SUBDIRS:
        candidate = cwd / subdir
        if (candidate / "package.json").exists():
            results.append((candidate, subdir))

    return results


def detect_verification_commands(cwd: Path) -> VerificationCommands:
    """
    Auto-detect verification commands based on project files.

    Checks for pyproject.toml (Python) or package.json (Node.js) and suggests
    appropriate commands for testing, type checking, and linting.

    Args:
        cwd: Project root directory.

    Returns:
        VerificationCommands with detected commands.
    """
    from gobby.project_verification.refresh import refresh_project_verification_deterministic

    result = refresh_project_verification_deterministic(cwd)
    if not result.after:
        logger.debug("No recognized project type detected")
    return VerificationCommands.from_dict(result.after)


def initialize_project(
    cwd: Path | None = None,
    name: str | None = None,
    github_url: str | None = None,
) -> InitResult:
    """
    Initialize a Gobby project in the given directory.

    If the project is already initialized (has .gobby/project.json),
    returns the existing project info. Otherwise creates a new project
    in the database and writes the local project.json file.

    Args:
        cwd: Directory to initialize. Defaults to current working directory.
        name: Project name. Defaults to directory name if not provided.
        github_url: GitHub URL. Auto-detected from git remote if not provided.

    Returns:
        InitResult with project details and whether it already existed.

    Raises:
        Exception: If project creation fails.
    """
    from gobby.storage.hub.runtime import open_runtime_hub_database
    from gobby.storage.projects import LocalProjectManager
    from gobby.utils.git import get_github_url as detect_github_url
    from gobby.utils.project_context import get_project_context

    if cwd is None:
        cwd = Path.cwd()

    cwd = cwd.resolve()

    # Check if already initialized
    project_context = get_project_context(cwd)
    if project_context and project_context.get("id"):
        logger.debug(f"Project already initialized: {project_context.get('name')}")

        # Re-detect and merge verification commands on re-init
        from gobby.project_verification.refresh import refresh_project_verification_deterministic

        refresh_result = refresh_project_verification_deterministic(cwd, fix=True)
        verification = VerificationCommands.from_dict(refresh_result.after)
        if refresh_result.written:
            logger.info("Updated verification commands in project.json")

        return InitResult(
            project_id=str(project_context["id"]),
            project_name=project_context.get("name", ""),
            project_path=project_context.get("project_path", str(cwd)),
            created_at=project_context.get("created_at", ""),
            already_existed=True,
            verification=verification if verification.to_dict() else None,
        )

    # Auto-detect name from directory if not provided
    if not name:
        name = cwd.name

    # Auto-detect GitHub URL from git remote if not provided
    if not github_url:
        github_url = detect_github_url(cwd)

    # Initialize database
    db = open_runtime_hub_database(apply_migrations=False)
    project_manager = LocalProjectManager(db)

    # Auto-detect verification commands
    verification = detect_verification_commands(cwd)

    # Check if project with same name exists in database
    existing = project_manager.get_by_name(name)
    if existing:
        # Project exists in DB but no local project.json - write it
        logger.debug(f"Found existing project in database: {name}")

        # Backfill repo_path if missing (e.g. project was created via GitHub)
        if not existing.repo_path:
            project_manager.update(existing.id, repo_path=str(cwd))
            logger.info(f"Updated repo_path for project '{name}' to {cwd}")

        _write_project_json(
            cwd,
            existing.id,
            existing.name,
            existing.created_at,
            verification,
            linear_team_id=_optional_str(existing.linear_team_id),
            linear_project_id=_optional_str(existing.linear_project_id),
        )
        return InitResult(
            project_id=existing.id,
            project_name=existing.name,
            project_path=str(cwd),
            created_at=existing.created_at,
            already_existed=True,
            verification=verification if verification.to_dict() else None,
        )

    # Create new project
    logger.debug(f"Creating new project: {name}")
    project = project_manager.create(
        name=name,
        repo_path=str(cwd),
        github_url=github_url,
    )

    # Write local .gobby/project.json
    _write_project_json(cwd, project.id, project.name, project.created_at, verification)

    logger.info(f"Initialized project '{name}' in {cwd}")

    return InitResult(
        project_id=project.id,
        project_name=project.name,
        project_path=str(cwd),
        created_at=project.created_at,
        already_existed=False,
        verification=verification if verification.to_dict() else None,
    )


def _update_project_json_verification(
    cwd: Path,
    verification: VerificationCommands,
) -> None:
    """Update verification commands in an existing project.json, preserving other fields."""
    project_file = cwd / ".gobby" / "project.json"
    if not project_file.exists():
        return

    try:
        with open(project_file) as f:
            project_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read project.json for update: {e}")
        return

    verification_dict = verification.to_dict()

    # Merge: detected commands override, but preserve manually-added custom entries
    existing_verification = project_data.get("verification", {})
    existing_custom = existing_verification.get("custom", {})

    # Merge custom entries: new detection wins, but keep manual entries not in detection
    merged_custom = {**existing_custom, **verification_dict.get("custom", {})}

    project_data["verification"] = {k: v for k, v in verification_dict.items() if k != "custom"}
    if merged_custom:
        project_data["verification"]["custom"] = merged_custom

    with open(project_file, "w") as f:
        json.dump(project_data, f, indent=2)

    logger.debug(f"Updated verification in {project_file}")


def update_project_json_fields(cwd: Path, **fields: Any) -> None:
    """Update top-level fields in .gobby/project.json, preserving other fields."""
    project_file = cwd / ".gobby" / "project.json"
    if not project_file.exists():
        return

    try:
        with open(project_file, encoding="utf-8") as f:
            project_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read project.json for field update: {e}")
        return

    for key, value in fields.items():
        project_data[key] = value

    with open(project_file, "w", encoding="utf-8") as f:
        json.dump(project_data, f, indent=2)
        f.write("\n")

    logger.debug(f"Updated project.json fields in {project_file}")


def _write_project_json(
    cwd: Path,
    project_id: str,
    name: str,
    created_at: str,
    verification: VerificationCommands | None = None,
    linear_team_id: str | None = None,
    linear_project_id: str | None = None,
) -> None:
    """Write the .gobby/project.json file.

    Args:
        cwd: Project root directory.
        project_id: Project ID.
        name: Project name.
        created_at: Project creation timestamp.
        verification: Optional verification commands to include.
    """
    gobby_dir = cwd / ".gobby"
    gobby_dir.mkdir(exist_ok=True)

    project_file = gobby_dir / "project.json"
    project_data: dict[str, Any] = {
        "id": project_id,
        "name": name,
        "created_at": created_at,
    }
    if linear_team_id is not None:
        project_data["linear_team_id"] = linear_team_id
    if linear_project_id is not None:
        project_data["linear_project_id"] = linear_project_id

    # Add verification config if provided and has commands
    if verification:
        verification_dict = verification.to_dict()
        if verification_dict:
            project_data["verification"] = verification_dict

    with open(project_file, "w") as f:
        json.dump(project_data, f, indent=2)
        f.write("\n")

    logger.debug(f"Wrote project.json to {project_file}")
