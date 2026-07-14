"""Dev mode detection utility."""

from pathlib import Path

__all__ = ["is_dev_mode", "is_gobby_project", "has_gobby_pyproject"]


def has_gobby_pyproject(path: Path) -> bool:
    """Check if a directory has a pyproject.toml for the gobby project.

    Weaker check than is_gobby_project — only requires pyproject.toml,
    not the full source tree. Used by the service installer which needs
    to detect dev mode before the source tree is fully built.

    Args:
        path: Directory to check

    Returns:
        True if pyproject.toml with name="gobby" exists
    """
    pyproject = path / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        content = pyproject.read_text(encoding="utf-8")
        return 'name = "gobby"' in content or "name = 'gobby'" in content
    except OSError:
        return False


def is_gobby_project(path: Path) -> bool:
    """Check if a directory is the gobby source repository.

    Looks for the canonical marker: src/gobby/install/shared/ directory
    AND a pyproject.toml with name = "gobby".

    Args:
        path: Directory to check

    Returns:
        True if the path is the gobby source repo root
    """
    if not (path / "src" / "gobby" / "install" / "shared").is_dir():
        return False
    pyproject = path / "pyproject.toml"
    if not pyproject.exists():
        return False
    try:
        content = pyproject.read_text(encoding="utf-8")
        return 'name = "gobby"' in content or "name = 'gobby'" in content
    except OSError:
        return False


def is_dev_mode(project_path: Path | None = None) -> bool:
    """Detect if running inside the gobby source repo.

    When the project IS the gobby source repo, bundled resources are editable
    directly (no copies needed). This is used to gate write access to
    scope='bundled' records in the database.

    Args:
        project_path: Path to check (defaults to cwd)

    Returns:
        True if the path is inside the gobby source repo
    """
    path = project_path or Path.cwd()
    return any(is_gobby_project(candidate) for candidate in (path, *path.parents))
