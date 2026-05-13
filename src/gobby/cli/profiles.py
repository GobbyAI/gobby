"""CLI commands for build profile registry editing."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, TypeGuard

import click

from gobby.config.build import DeliveryMode, Isolation
from gobby.storage.build_profiles import (
    BuildProfileError,
    BuildProfileLoader,
    BuildProfileManager,
    BuildProfileSource,
)
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

logger = logging.getLogger(__name__)


@contextmanager
def _open_manager(*, sync: bool = True) -> Iterator[BuildProfileManager]:
    db = LocalDatabase()
    try:
        run_migrations(db)
        if sync:
            BuildProfileLoader().sync(db)
        manager = BuildProfileManager(db)
    except (OSError, RuntimeError, sqlite3.Error, BuildProfileError) as exc:
        logger.exception(
            "Failed to open build profile manager",
            extra={"sync": sync, "error_type": type(exc).__name__},
        )
        db.close()
        raise
    except Exception:
        db.close()
        raise
    try:
        yield manager
    finally:
        db.close()


def _scope(source: str, project_id: str | None) -> str | None:
    return None if source == "installed" else project_id


def _is_profile_source(source: str) -> TypeGuard[BuildProfileSource]:
    return source in {"installed", "project"}


def _profile_source(source: str) -> BuildProfileSource:
    if not _is_profile_source(source):
        raise click.ClickException(f"Invalid build profile source: {source}")
    return source


def _parse_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _echo(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True))


@click.group("profiles")
def profiles() -> None:
    """Manage build profiles."""


@profiles.command("list")
@click.option("--project-id")
@click.option("--include-deleted", is_flag=True, default=False)
def list_profiles(project_id: str | None, include_deleted: bool) -> None:
    with _open_manager(sync=False) as manager:
        _echo(
            [
                asdict(profile)
                for profile in manager.list_profiles(
                    project_id=project_id,
                    include_deleted=include_deleted,
                )
            ]
        )


@profiles.command("show")
@click.argument("name")
@click.option("--source", type=click.Choice(["installed", "project"]), default="installed")
@click.option("--project-id")
@click.option("--include-deleted", is_flag=True, default=False)
def show_profile(name: str, source: str, project_id: str | None, include_deleted: bool) -> None:
    with _open_manager(sync=False) as manager:
        profile = manager.get(
            name,
            source=_profile_source(source),
            project_id=_scope(source, project_id),
            include_deleted=include_deleted,
        )
        if profile is None:
            raise click.ClickException(f"Unknown build profile '{name}'")
        _echo(asdict(profile))


@profiles.command("create")
@click.argument("name")
@click.option("--label", "display_label", required=True)
@click.option("--description", required=True)
@click.option("--skip-stages")
@click.option("--isolation", type=click.Choice(["none", "worktree", "clone"]), default="worktree")
@click.option("--unattended/--no-unattended", default=False)
@click.option("--delivery-mode", type=click.Choice(["auto", "pull_request"]), default="auto")
@click.option("--delivery-target-repo")
@click.option("--enabled/--disabled", default=True)
@click.option("--source", type=click.Choice(["installed", "project"]), default="project")
@click.option("--project-id")
@click.option("--tags")
def create_profile(
    name: str,
    display_label: str,
    description: str,
    skip_stages: str | None,
    isolation: Isolation,
    unattended: bool,
    delivery_mode: DeliveryMode,
    delivery_target_repo: str | None,
    enabled: bool,
    source: str,
    project_id: str | None,
    tags: str | None,
) -> None:
    try:
        with _open_manager() as manager:
            profile = manager.create(
                name=name,
                display_label=display_label,
                description=description,
                skip_stages=_parse_csv(skip_stages),
                isolation=isolation,
                unattended=unattended,
                delivery_mode=delivery_mode,
                delivery_target_repo=delivery_target_repo,
                enabled=enabled,
                source=_profile_source(source),
                project_id=_scope(source, project_id),
                tags=_parse_csv(tags),
            )
            _echo(asdict(profile))
    except BuildProfileError as e:
        raise click.ClickException(str(e)) from e


def _profile_toggle(name: str, source: str, project_id: str | None, enabled: bool) -> None:
    try:
        with _open_manager() as manager:
            profile = manager.set_enabled(
                name,
                source=_profile_source(source),
                project_id=_scope(source, project_id),
                enabled=enabled,
            )
            _echo(asdict(profile))
    except BuildProfileError as e:
        raise click.ClickException(str(e)) from e


@profiles.command("update")
@click.argument("name")
@click.option("--source", type=click.Choice(["installed", "project"]), default="project")
@click.option("--project-id")
@click.option("--label", "display_label")
@click.option("--description")
@click.option("--skip-stages")
@click.option("--isolation", type=click.Choice(["none", "worktree", "clone"]))
@click.option("--unattended/--no-unattended", default=None)
@click.option("--delivery-mode", type=click.Choice(["auto", "pull_request"]))
@click.option("--delivery-target-repo")
@click.option("--enabled/--disabled", default=None)
@click.option("--tags")
def update_profile(
    name: str,
    source: str,
    project_id: str | None,
    display_label: str | None,
    description: str | None,
    skip_stages: str | None,
    isolation: Isolation | None,
    unattended: bool | None,
    delivery_mode: DeliveryMode | None,
    delivery_target_repo: str | None,
    enabled: bool | None,
    tags: str | None,
) -> None:
    updates: dict[str, object] = {}
    if display_label is not None:
        updates["display_label"] = display_label
    if description is not None:
        updates["description"] = description
    if skip_stages is not None:
        updates["skip_stages"] = _parse_csv(skip_stages)
    if isolation is not None:
        updates["isolation"] = isolation
    if unattended is not None:
        updates["unattended"] = unattended
    if delivery_mode is not None:
        updates["delivery_mode"] = delivery_mode
    if delivery_target_repo is not None:
        # Click passes None when the option is omitted; only an explicit empty
        # value should clear the stored delivery target.
        updates["delivery_target_repo"] = delivery_target_repo or None
    if enabled is not None:
        updates["enabled"] = enabled
    if tags is not None:
        updates["tags"] = _parse_csv(tags)
    try:
        with _open_manager() as manager:
            profile = manager.update(
                name,
                source=_profile_source(source),
                project_id=_scope(source, project_id),
                updates=updates,
            )
            _echo(asdict(profile))
    except BuildProfileError as e:
        raise click.ClickException(str(e)) from e


@profiles.command("enable")
@click.argument("name")
@click.option("--source", type=click.Choice(["installed", "project"]), default="project")
@click.option("--project-id")
def enable_profile(name: str, source: str, project_id: str | None) -> None:
    _profile_toggle(name, source, project_id, True)


@profiles.command("disable")
@click.argument("name")
@click.option("--source", type=click.Choice(["installed", "project"]), default="project")
@click.option("--project-id")
def disable_profile(name: str, source: str, project_id: str | None) -> None:
    _profile_toggle(name, source, project_id, False)


@profiles.command("restore")
@click.argument("name")
@click.option("--source", type=click.Choice(["installed", "project"]), default="installed")
@click.option("--project-id")
def restore_profile(name: str, source: str, project_id: str | None) -> None:
    try:
        with _open_manager() as manager:
            _echo(
                asdict(
                    manager.restore(
                        name,
                        source=_profile_source(source),
                        project_id=_scope(source, project_id),
                    )
                )
            )
    except BuildProfileError as e:
        raise click.ClickException(str(e)) from e


@profiles.command("delete")
@click.argument("name")
@click.option("--source", type=click.Choice(["installed", "project"]), default="project")
@click.option("--project-id")
@click.option("--purge", is_flag=True, default=False)
def delete_profile(name: str, source: str, project_id: str | None, purge: bool) -> None:
    try:
        with _open_manager() as manager:
            profile = manager.delete(
                name,
                source=_profile_source(source),
                project_id=_scope(source, project_id),
                purge=purge,
            )
            _echo(asdict(profile) if profile is not None else {"deleted": True})
    except BuildProfileError as e:
        raise click.ClickException(str(e)) from e
