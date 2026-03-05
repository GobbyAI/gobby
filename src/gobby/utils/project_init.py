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


@dataclass
class VerificationCommands:
    """Auto-detected verification commands for a project."""

    unit_tests: str | None = None
    type_check: str | None = None
    lint: str | None = None
    format: str | None = None
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
        if self.integration:
            result["integration"] = self.integration
        if self.custom:
            result["custom"] = self.custom
        return result


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
    verification = VerificationCommands()
    detected_any = False

    # Check for Python project (pyproject.toml)
    pyproject_path = cwd / "pyproject.toml"
    if pyproject_path.exists():
        logger.debug("Detected Python project (pyproject.toml)")
        detected_any = True

        # Check for tests directory
        tests_dir = cwd / "tests"
        if tests_dir.exists() and tests_dir.is_dir():
            verification.unit_tests = "uv run pytest tests/ -v"

        # Check for src directory (common pattern)
        src_dir = cwd / "src"
        if src_dir.exists() and src_dir.is_dir():
            verification.type_check = "uv run mypy src/"
            verification.lint = "uv run ruff check src/"
            verification.format = "uv run ruff format --check src/"
        else:
            # Fall back to current directory
            verification.type_check = "uv run mypy ."
            verification.lint = "uv run ruff check ."
            verification.format = "uv run ruff format --check ."

    # Check for Node.js project (package.json) — root and common subdirectories
    frontend_dirs = _find_frontend_dirs(cwd)
    for frontend_dir, subdir_name in frontend_dirs:
        logger.debug(f"Detected Node.js project ({subdir_name}/package.json)")
        detected_any = True

        try:
            with open(frontend_dir / "package.json") as f:
                package_data = json.load(f)

            scripts = package_data.get("scripts", {})
            # Commands need cd prefix if in a subdirectory
            prefix = f"cd {subdir_name} && " if subdir_name != "." else ""

            if verification.unit_tests:
                # Another language already claimed primary slots — use custom
                if "test" in scripts:
                    verification.custom["frontend_tests"] = f"{prefix}npm test"
                if "lint" in scripts:
                    verification.custom["frontend_lint"] = f"{prefix}npm run lint"
                for script_name in ["type-check", "typecheck", "types", "tsc"]:
                    if script_name in scripts:
                        verification.custom["ts_check"] = f"{prefix}npm run {script_name}"
                        break
            else:
                # Node.js is the primary language
                if "test" in scripts:
                    verification.unit_tests = f"{prefix}npm test"
                if "lint" in scripts:
                    verification.lint = f"{prefix}npm run lint"
                for script_name in ["type-check", "typecheck", "types", "tsc"]:
                    if script_name in scripts:
                        verification.type_check = f"{prefix}npm run {script_name}"
                        break

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to parse {subdir_name}/package.json: {e}")

    # Check for Rust project (Cargo.toml)
    cargo_path = cwd / "Cargo.toml"
    if cargo_path.exists():
        logger.debug("Detected Rust project (Cargo.toml)")
        detected_any = True
        if not verification.unit_tests:
            verification.unit_tests = "cargo test"
            verification.lint = "cargo clippy"
        else:
            verification.custom["cargo_test"] = "cargo test"
            verification.custom["clippy"] = "cargo clippy"

    # Check for Go project (go.mod)
    go_mod_path = cwd / "go.mod"
    if go_mod_path.exists():
        logger.debug("Detected Go project (go.mod)")
        detected_any = True
        if not verification.unit_tests:
            verification.unit_tests = "go test ./..."
            verification.lint = "go vet ./..."
        else:
            verification.custom["go_test"] = "go test ./..."
            verification.custom["go_vet"] = "go vet ./..."

    if not detected_any:
        logger.debug("No recognized project type detected")

    return verification


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

        # Re-detect and update verification commands on re-init
        verification = detect_verification_commands(cwd)
        if verification.to_dict():
            _update_project_json_verification(cwd, verification)
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

    # Initialize database and run migrations
    db = LocalDatabase()
    run_migrations(db)
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

        _write_project_json(cwd, existing.id, existing.name, existing.created_at, verification)
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


def _write_project_json(
    cwd: Path,
    project_id: str,
    name: str,
    created_at: str,
    verification: VerificationCommands | None = None,
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

    # Add verification config if provided and has commands
    if verification:
        verification_dict = verification.to_dict()
        if verification_dict:
            project_data["verification"] = verification_dict

    with open(project_file, "w") as f:
        json.dump(project_data, f, indent=2)

    logger.debug(f"Wrote project.json to {project_file}")
