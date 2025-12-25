"""
Shared project initialization utilities.

This module provides the core logic for initializing a Gobby project,
used by both the CLI and the hook system for auto-initialization.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class InitResult:
    """Result of project initialization."""

    project_id: str
    project_name: str
    project_path: str
    created_at: str
    already_existed: bool


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
    from gobby.storage.database import LocalDatabase
    from gobby.storage.migrations import run_migrations
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
        return InitResult(
            project_id=str(project_context["id"]),
            project_name=project_context.get("name", ""),
            project_path=project_context.get("project_path", str(cwd)),
            created_at=project_context.get("created_at", ""),
            already_existed=True,
        )

    # Auto-detect name from directory if not provided
    if not name:
        name = cwd.name

    # Auto-detect GitHub URL from git remote if not provided
    if not github_url:
        github_url = detect_github_url(cwd)

    # Initialize database and run migrations
    db = LocalDatabase()
    run_migrations(db)
    project_manager = LocalProjectManager(db)

    # Check if project with same name exists in database
    existing = project_manager.get_by_name(name)
    if existing:
        # Project exists in DB but no local project.json - write it
        logger.debug(f"Found existing project in database: {name}")
        _write_project_json(cwd, existing.id, existing.name, existing.created_at)
        return InitResult(
            project_id=existing.id,
            project_name=existing.name,
            project_path=str(cwd),
            created_at=existing.created_at,
            already_existed=True,
        )

    # Create new project
    logger.debug(f"Creating new project: {name}")
    project = project_manager.create(
        name=name,
        repo_path=str(cwd),
        github_url=github_url,
    )

    # Write local .gobby/project.json
    _write_project_json(cwd, project.id, project.name, project.created_at)

    logger.info(f"Initialized project '{name}' in {cwd}")

    return InitResult(
        project_id=project.id,
        project_name=project.name,
        project_path=str(cwd),
        created_at=project.created_at,
        already_existed=False,
    )


def _write_project_json(cwd: Path, project_id: str, name: str, created_at: str) -> None:
    """Write the .gobby/project.json file."""
    gobby_dir = cwd / ".gobby"
    gobby_dir.mkdir(exist_ok=True)

    project_file = gobby_dir / "project.json"
    project_data = {
        "id": project_id,
        "name": name,
        "created_at": created_at,
    }

    with open(project_file, "w") as f:
        json.dump(project_data, f, indent=2)

    logger.debug(f"Wrote project.json to {project_file}")
