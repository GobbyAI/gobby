"""CLI commands for stage registry editing."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import click

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.tasks import StageRegistryManager


def _open_manager() -> tuple[LocalDatabase, StageRegistryManager]:
    db = LocalDatabase()
    try:
        run_migrations(db)
        return db, StageRegistryManager(db)
    except Exception:
        db.close()
        raise


def _echo(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True))


@click.group("stages")
def stages() -> None:
    """Manage lifecycle stage registry rows."""


@stages.command("list")
@click.option("--include-deleted", is_flag=True, default=False)
def list_stages(include_deleted: bool) -> None:
    db, manager = _open_manager()
    try:
        _echo([asdict(entry) for entry in manager.list_all(include_deleted=include_deleted)])
    finally:
        db.close()


@stages.command("show")
@click.argument("name")
@click.option("--include-deleted", is_flag=True, default=False)
def show_stage(name: str, include_deleted: bool) -> None:
    db, manager = _open_manager()
    try:
        entry = manager.get(name, include_deleted=include_deleted)
        if entry is None:
            raise click.ClickException(f"Unknown stage '{name}'")
        _echo(asdict(entry))
    finally:
        db.close()


@stages.command("update")
@click.argument("name")
@click.option("--label", "display_label")
@click.option("--description")
@click.option(
    "--category",
    type=click.Choice(["discovery", "design", "verification", "implementation", "delivery"]),
)
@click.option("--default-agent")
@click.option("--reviewer-agent")
@click.option("--reviewer-agent-selector-json")
@click.option("--review-policy", type=click.Choice(["none", "required", "optional"]))
@click.option("--dispatch-type", type=click.Choice(["agent", "pipeline"]))
@click.option("--dispatch-target")
@click.option("--dispatch-inputs-json")
@click.option("--position-hint", type=int)
@click.option("--requires-human/--no-requires-human", default=None)
@click.option("--terminal/--no-terminal", "is_terminal", default=None)
@click.option("--default-max-work-attempts", type=int)
@click.option("--default-max-review-rounds", type=int)
def update_stage(
    name: str,
    display_label: str | None,
    description: str | None,
    category: str | None,
    default_agent: str | None,
    reviewer_agent: str | None,
    reviewer_agent_selector_json: str | None,
    review_policy: str | None,
    dispatch_type: str | None,
    dispatch_target: str | None,
    dispatch_inputs_json: str | None,
    position_hint: int | None,
    requires_human: bool | None,
    is_terminal: bool | None,
    default_max_work_attempts: int | None,
    default_max_review_rounds: int | None,
) -> None:
    updates: dict[str, object] = {}
    for key, value in {
        "display_label": display_label,
        "description": description,
        "category": category,
        "default_agent": default_agent,
        "reviewer_agent": reviewer_agent,
        "reviewer_agent_selector_json": reviewer_agent_selector_json,
        "review_policy": review_policy,
        "dispatch_type": dispatch_type,
        "dispatch_target": dispatch_target,
        "dispatch_inputs_json": dispatch_inputs_json,
        "position_hint": position_hint,
        "requires_human": requires_human,
        "is_terminal": is_terminal,
        "default_max_work_attempts": default_max_work_attempts,
        "default_max_review_rounds": default_max_review_rounds,
    }.items():
        if value is not None:
            updates[key] = value
    db, manager = _open_manager()
    try:
        _echo(asdict(manager.update_stage(name, updates)))
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    finally:
        db.close()


@stages.command("restore")
@click.argument("name")
def restore_stage(name: str) -> None:
    db, manager = _open_manager()
    try:
        _echo(asdict(manager.restore_stage(name)))
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    finally:
        db.close()


@stages.command("delete")
@click.argument("name")
def delete_stage(name: str) -> None:
    db, manager = _open_manager()
    try:
        _echo(asdict(manager.delete_stage(name)))
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    finally:
        db.close()


@stages.command("defaults")
@click.argument("task_type")
@click.option(
    "--set",
    "stage_values",
    multiple=True,
    help="stage:position entry. May be repeated.",
)
def defaults(task_type: str, stage_values: tuple[str, ...]) -> None:
    db, manager = _open_manager()
    try:
        if stage_values:
            parsed: list[tuple[str, int]] = []
            for raw in stage_values:
                stage_name, separator, position_text = raw.partition(":")
                if not separator:
                    raise click.ClickException("--set must use stage:position")
                try:
                    position = int(position_text)
                except ValueError as exc:
                    raise click.ClickException("--set position must be an integer") from exc
                parsed.append((stage_name, position))
            manager.set_default_stages(task_type, parsed)
        _echo(
            [
                {"stage_name": stage_name, "position": position}
                for stage_name, position in manager.list_default_stages(task_type)
            ]
        )
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    finally:
        db.close()
