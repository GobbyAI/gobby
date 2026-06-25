"""Skill metadata CLI operations."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import click

from gobby.storage.skills import LocalSkillManager

StorageFactory = Callable[[], LocalSkillManager]
_FACADE_MODULE = "gobby.cli.skills"


def _facade() -> Any:
    return sys.modules[_FACADE_MODULE]


def get_metadata(storage_factory: StorageFactory, name: str, key: str) -> None:
    """Get a metadata field value."""
    storage = storage_factory()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    if not skill.metadata:
        click.echo("null")
        return

    value = _facade().get_nested_value(skill.metadata, key)
    if value is None:
        click.echo(f"Key not found: {key}")
        sys.exit(1)
    if isinstance(value, dict | list):
        click.echo(json.dumps(value, indent=2))
    else:
        click.echo(str(value))


def set_metadata(storage_factory: StorageFactory, name: str, key: str, value: str) -> None:
    """Set a metadata field value."""
    storage = storage_factory()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    try:
        parsed_value: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed_value = value

    new_metadata = _facade().set_nested_value(skill.metadata or {}, key, parsed_value)
    try:
        storage.update_skill(skill.id, metadata=new_metadata)
    except (OSError, RuntimeError, ValueError) as exc:
        click.echo(f"Error updating skill metadata: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Set {key} = {value}")


def unset_metadata(storage_factory: StorageFactory, name: str, key: str) -> None:
    """Remove a metadata field."""
    storage = storage_factory()
    skill = storage.get_by_name(name)

    if skill is None:
        click.echo(f"Skill not found: {name}", err=True)
        sys.exit(1)

    if not skill.metadata:
        click.echo(f"Key not found: {key}")
        return

    new_metadata = _facade().unset_nested_value(skill.metadata, key)
    try:
        storage.update_skill(skill.id, metadata=new_metadata)
    except (OSError, RuntimeError, ValueError) as exc:
        click.echo(f"Error updating skill metadata: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Unset {key}")
