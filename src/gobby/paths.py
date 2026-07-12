"""
Core path utilities for Gobby package.

This module provides stable path resolution utilities that work in both
development (source) and installed (package) modes without CLI dependencies.
"""

import os
from pathlib import Path

__all__ = [
    "get_package_root",
    "get_install_dir",
    "get_gobby_home",
    "get_global_workflows_dir",
    "get_global_rules_dir",
    "get_global_pipelines_dir",
    "get_global_agents_dir",
    "get_global_variables_dir",
    "get_project_workflows_dir",
    "get_project_rules_dir",
    "get_project_pipelines_dir",
    "get_project_agents_dir",
    "get_project_variables_dir",
]


def get_package_root() -> Path:
    """Get the root directory of the gobby package.

    Returns:
        Path to src/gobby/ (the package root directory)
    """
    import gobby

    return Path(gobby.__file__).parent


def get_install_dir() -> Path:
    """Get the gobby install directory.

    Checks for source directory (development mode) first,
    falls back to package directory. This handles both:
    - Development: src/gobby/install/
    - Installed package: <site-packages>/gobby/install/

    Returns:
        Path to the install directory
    """
    import gobby

    package_install_dir = Path(gobby.__file__).parent / "install"

    # Try to find source directory (project root) for development mode
    current = Path(gobby.__file__).resolve()
    source_install_dir = None

    for parent in current.parents:
        potential_source = parent / "src" / "gobby" / "install"
        if potential_source.exists():
            source_install_dir = potential_source
            break

    if source_install_dir and source_install_dir.exists():
        return source_install_dir
    return package_install_dir


# ---------------------------------------------------------------------------
# Global user template directories (~/.gobby/workflows/<type>/)
# ---------------------------------------------------------------------------


def get_gobby_home() -> Path:
    """Get the gobby home directory (~/.gobby or $GOBBY_HOME)."""
    configured_home = os.environ.get("GOBBY_HOME")
    if configured_home is None or not configured_home.strip():
        return Path.home() / ".gobby"
    return Path(configured_home).expanduser()


def get_global_workflows_dir() -> Path:
    """Get the global user workflows root: ~/.gobby/workflows/."""
    return get_gobby_home() / "workflows"


def get_global_rules_dir() -> Path:
    """Get the global user rules directory: ~/.gobby/workflows/rules/."""
    return get_global_workflows_dir() / "rules"


def get_global_pipelines_dir() -> Path:
    """Get the global user pipelines directory: ~/.gobby/workflows/pipelines/."""
    return get_global_workflows_dir() / "pipelines"


def get_global_agents_dir() -> Path:
    """Get the global user agents directory: ~/.gobby/workflows/agents/."""
    return get_global_workflows_dir() / "agents"


def get_global_variables_dir() -> Path:
    """Get the global user variables directory: ~/.gobby/workflows/variables/."""
    return get_global_workflows_dir() / "variables"


# ---------------------------------------------------------------------------
# Project-scoped template directories (.gobby/workflows/<type>/)
# ---------------------------------------------------------------------------


def get_project_workflows_dir(project_path: Path) -> Path:
    """Get project workflows root: <project>/.gobby/workflows/."""
    return project_path / ".gobby" / "workflows"


def get_project_rules_dir(project_path: Path) -> Path:
    """Get project rules directory: <project>/.gobby/workflows/rules/."""
    return get_project_workflows_dir(project_path) / "rules"


def get_project_pipelines_dir(project_path: Path) -> Path:
    """Get project pipelines directory: <project>/.gobby/workflows/pipelines/."""
    return get_project_workflows_dir(project_path) / "pipelines"


def get_project_agents_dir(project_path: Path) -> Path:
    """Get project agents directory: <project>/.gobby/workflows/agents/."""
    return get_project_workflows_dir(project_path) / "agents"


def get_project_variables_dir(project_path: Path) -> Path:
    """Get project variables directory: <project>/.gobby/workflows/variables/."""
    return get_project_workflows_dir(project_path) / "variables"
