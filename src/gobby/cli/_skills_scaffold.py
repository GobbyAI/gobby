"""Skill scaffold CLI operations."""

from __future__ import annotations

import sys
from pathlib import Path

import click


def init_skills() -> None:
    """Initialize skills directory for the current project."""
    from gobby.skills.scaffold import init_skills_directory

    base_path = Path(".")
    skills_dir = base_path / ".gobby" / "skills"
    config_file = skills_dir / "config.yaml"

    result = init_skills_directory(base_path)

    if result["dir_created"]:
        click.echo(f"Created {skills_dir}/")
    else:
        click.echo(f"Skills directory already exists: {skills_dir}/")

    if result["config_created"]:
        click.echo(f"Created {config_file}")
    else:
        click.echo(f"Config already exists: {config_file}")

    click.echo("\nSkills initialized successfully!")


def create_skill(name: str, description: str | None) -> None:
    """Create a new skill scaffold."""
    from gobby.skills.scaffold import scaffold_skill

    try:
        scaffold_skill(name, Path("."), description)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except FileExistsError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    click.echo(f"Created skill scaffold: {name}/")
    click.echo(f"  - {name}/SKILL.md")
    click.echo(f"  - {name}/scripts/")
    click.echo(f"  - {name}/assets/")
    click.echo(f"  - {name}/references/")
