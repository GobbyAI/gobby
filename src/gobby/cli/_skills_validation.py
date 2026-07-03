"""Skill validation CLI operations."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from gobby.utils.json_helpers import json_dumps


def validate_skill(path: str, json_output: bool) -> None:
    """Validate a SKILL.md file against the Agent Skills specification."""
    from gobby.skills.loader import SkillLoader, SkillLoadError
    from gobby.skills.validator import SkillValidator

    source_path = Path(path)

    if not source_path.exists():
        if json_output:
            click.echo(json_dumps({"error": "Path not found", "path": path}))
        else:
            click.echo(f"Error: Path not found: {path}")
        sys.exit(1)

    loader = SkillLoader()
    try:
        parsed_skill = loader.load_skill(source_path, validate=False, check_dir_name=False)
    except SkillLoadError as exc:
        if json_output:
            click.echo(json_dumps({"error": str(exc), "path": path}))
        else:
            click.echo(f"Error loading skill: {exc}")
        sys.exit(1)

    result = SkillValidator().validate(parsed_skill)

    if json_output:
        output = result.to_dict()
        output["path"] = path
        output["skill_name"] = parsed_skill.name
        click.echo(json_dumps(output, indent=2))
        if not result.valid:
            sys.exit(1)
        return

    if result.valid:
        click.echo(f"✓ Valid: {parsed_skill.name}")
        if result.warnings:
            click.echo("\nWarnings:")
            for warning in result.warnings:
                click.echo(f"  - {warning}")
        return

    click.echo(f"✗ Invalid: {parsed_skill.name}")
    click.echo("\nErrors:")
    for error in result.errors:
        click.echo(f"  - {error}")
    if result.warnings:
        click.echo("\nWarnings:")
        for warning in result.warnings:
            click.echo(f"  - {warning}")
    sys.exit(1)
