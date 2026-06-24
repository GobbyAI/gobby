"""Local storage-backed skills CLI operations."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from gobby.skills.metadata import get_skill_category, get_skill_tags
from gobby.storage.skills import LocalSkillManager

StorageFactory = Callable[[], LocalSkillManager]
JsonOutput = Callable[[list[Any]], None]


def output_json(skills_list: list[Any]) -> None:
    """Output skills as JSON."""
    from gobby.skills.formatting import format_skills_json

    click.echo(format_skills_json(skills_list))


def list_skills(
    storage_factory: StorageFactory,
    output_json_callback: JsonOutput,
    category: str | None,
    tags: str | None,
    enabled: bool | None,
    limit: int,
    json_output: bool,
) -> None:
    """List installed skills."""
    storage = storage_factory()
    fetch_limit = 10000 if tags else limit

    skills_list = storage.list_skills(
        category=category,
        enabled=enabled,
        limit=fetch_limit,
        include_global=True,
    )

    if tags:
        tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
        if tags_list:
            filtered_skills = []
            for skill in skills_list:
                skill_tags = get_skill_tags(skill)
                if any(tag in skill_tags for tag in tags_list):
                    filtered_skills.append(skill)
            skills_list = filtered_skills[:limit]

    if json_output:
        output_json_callback(skills_list)
        return

    if not skills_list:
        click.echo("No skills found.")
        return

    for skill in skills_list:
        category_suffix = ""
        skill_category = get_skill_category(skill)
        if skill_category:
            category_suffix = f" [{skill_category}]"

        status = "✓" if skill.enabled else "✗"
        desc = skill.description[:60] if skill.description else ""
        click.echo(f"{status} {skill.name}{category_suffix} - {desc}")


def show_skill(storage_factory: StorageFactory, name: str, json_output: bool) -> None:
    """Show details of a specific skill."""
    storage = storage_factory()
    skill = storage.get_by_name(name)

    if skill is None:
        if json_output:
            click.echo(json.dumps({"error": "Skill not found", "name": name}))
        else:
            click.echo(f"Skill not found: {name}")
        sys.exit(1)

    if json_output:
        output = {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "license": skill.license,
            "enabled": skill.enabled,
            "source_type": skill.source_type,
            "source_path": skill.source_path,
            "compatibility": skill.compatibility if hasattr(skill, "compatibility") else None,
            "content": skill.content,
            "category": get_skill_category(skill),
            "tags": get_skill_tags(skill),
        }
        click.echo(json.dumps(output, indent=2))
        return

    click.echo(f"Name: {skill.name}")
    click.echo(f"Description: {skill.description}")
    if skill.version:
        click.echo(f"Version: {skill.version}")
    if skill.license:
        click.echo(f"License: {skill.license}")
    click.echo(f"Enabled: {skill.enabled}")
    if skill.source_type:
        click.echo(f"Source: {skill.source_type}")
    if skill.source_path:
        click.echo(f"Path: {skill.source_path}")
    click.echo("")
    click.echo("Content:")
    click.echo("-" * 40)
    click.echo(skill.content)


def generate_docs(
    storage_factory: StorageFactory,
    output: str | None,
    output_format: str,
) -> None:
    """Generate documentation for installed skills."""
    storage = storage_factory()
    skills_list = storage.list_skills(include_global=True)

    if not skills_list:
        click.echo("No skills installed.")
        return

    from gobby.skills.formatting import format_skills_json, format_skills_markdown_table

    if output_format == "json":
        content = format_skills_json(skills_list)
    else:
        content = format_skills_markdown_table(skills_list)

    if output:
        try:
            Path(output).write_text(content, encoding="utf-8")
        except OSError as exc:
            click.echo(f"Error: Failed to write documentation to {output}: {exc}", err=True)
            sys.exit(1)
        click.echo(f"Written to {output}")
    else:
        click.echo(content)


def set_skill_enabled(storage_factory: StorageFactory, name: str, enabled: bool) -> None:
    """Enable or disable a skill."""
    storage = storage_factory()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    action = "enabling" if enabled else "disabling"
    try:
        storage.update_skill(skill.id, enabled=enabled)
    except (OSError, RuntimeError, ValueError) as exc:
        click.echo(f"Error {action} skill: {exc}", err=True)
        sys.exit(1)

    past_tense = "Enabled" if enabled else "Disabled"
    click.echo(f"{past_tense} skill: {name}")
