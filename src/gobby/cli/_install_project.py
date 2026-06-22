"""Project initialization helpers for the install command."""

import sys
from pathlib import Path

import click

from gobby.utils.project_init import initialize_project


def _is_git_root_without_gobby_project(project_path: Path) -> bool:
    if not (project_path / ".git").exists():
        return False
    return not (project_path / ".gobby" / "project.json").exists()


def _should_initialize_project(project_path: Path, *, no_interactive: bool) -> bool:
    if not _is_git_root_without_gobby_project(project_path):
        return False
    if no_interactive:
        return True
    if not sys.stdin.isatty():
        return False
    return click.confirm(
        "This git root is not a Gobby project yet. Initialize it now?",
        default=True,
    )


def _initialize_project_after_setup(project_path: Path) -> None:
    result = initialize_project(cwd=project_path)
    if result.already_existed:
        click.echo(f"Gobby project already initialized: {result.project_name}")
    else:
        click.echo(f"Initialized Gobby project: {result.project_name}")
    click.echo(f"  Project ID: {result.project_id}")
