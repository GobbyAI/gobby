"""CLI surface for build lifecycle automation."""

from __future__ import annotations

import asyncio
from typing import cast

import click

from gobby.build import (
    BuildControlResult,
    BuildOptions,
    BuildResult,
    StageInsertion,
    build,
    build_resume,
    build_stop,
)
from gobby.config.build import Isolation, StageCapOverride
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

from .utils import resolve_project_ref


def resolve_project_id() -> str:
    """Resolve the current project id for build requests."""
    project_id = resolve_project_ref(None, exit_on_not_found=False)
    if project_id is None:
        raise click.ClickException("No project context found")
    return project_id


def invoke_build_skill() -> None:
    """Invoke the interactive build skill path."""
    click.echo("No build input provided. Invoke the build skill from your active Gobby session.")


def _parse_stage_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    stages = [stage.strip() for stage in raw.split(",") if stage.strip()]
    return stages or None


def _parse_skip_stages(raw_values: tuple[str, ...]) -> list[str]:
    stages: list[str] = []
    for raw in raw_values:
        stages.extend(stage.strip() for stage in raw.split(",") if stage.strip())
    return stages


def _parse_add_stage(raw_values: tuple[str, ...]) -> list[StageInsertion]:
    insertions: list[StageInsertion] = []
    for raw in raw_values:
        stage_name, separator, position_text = raw.partition("@")
        stage_name = stage_name.strip()
        if not stage_name:
            raise click.ClickException("--add-stage requires a stage name")
        position = None
        if separator:
            try:
                position = int(position_text)
            except ValueError as exc:
                raise click.ClickException("--add-stage position must be an integer") from exc
        insertions.append(StageInsertion(stage_name=stage_name, position=position))
    return insertions


def _parse_stage_cap(raw_values: tuple[str, ...]) -> list[StageCapOverride]:
    by_stage: dict[str, dict[str, int | None]] = {}
    for raw in raw_values:
        stage_name, separator, cap_text = raw.partition(":")
        if not separator or not stage_name.strip():
            raise click.ClickException("--stage must use <stage>:<cap>=<value>")
        cap_name, cap_separator, value_text = cap_text.partition("=")
        if not cap_separator:
            raise click.ClickException("--stage cap must use <name>=<value>")
        cap_name = cap_name.strip()
        if cap_name not in {"max_work_attempts", "max_review_rounds"}:
            raise click.ClickException("--stage cap must be max_work_attempts or max_review_rounds")
        try:
            value = int(value_text)
        except ValueError as exc:
            raise click.ClickException("--stage cap value must be an integer") from exc
        stage_caps = by_stage.setdefault(stage_name.strip(), {})
        stage_caps[cap_name] = value
    return [
        StageCapOverride(
            stage_name=stage_name,
            max_work_attempts=values.get("max_work_attempts"),
            max_review_rounds=values.get("max_review_rounds"),
        )
        for stage_name, values in by_stage.items()
    ]


def _echo_build_result(result: BuildResult) -> None:
    click.echo(f"Task: {result.task_id}")
    click.echo(f"Lifecycle: {result.initial_lifecycle}")
    if result.applied_stages_skipped:
        click.echo(f"Skipped stages: {', '.join(result.applied_stages_skipped)}")
    click.echo(f"Dispatcher tick: {result.tick_dispatched}")


def _echo_build_control_result(result: BuildControlResult) -> None:
    state = "enabled" if result.enabled else "disabled"
    click.echo(f"Dispatcher cron: {state}")
    click.echo(f"Project: {result.project_id}")
    click.echo(f"Event: {result.lifecycle_event.reason}")


def _open_database() -> LocalDatabase:
    """Open the hub database and apply pending migrations before build storage use."""
    db = LocalDatabase()
    try:
        run_migrations(db)
    except Exception:
        db.close()
        raise
    return db


@click.command("build")
@click.argument("input_ref", required=False, metavar="[INPUT]")
@click.option("--profile", help="Build profile to apply.")
@click.option("--stages", help="Comma-separated explicit stage manifest.")
@click.option("--add-stage", multiple=True, help="Add a stage, optionally as name@position.")
@click.option(
    "--skip-stage", multiple=True, help="Stage to skip. May be repeated or comma-separated."
)
@click.option(
    "--stage",
    "stage_cap",
    multiple=True,
    help="Per-stage cap override, e.g. development:max_review_rounds=4.",
)
@click.option(
    "--isolation",
    type=click.Choice(["none", "worktree", "clone"]),
    default="worktree",
    show_default=True,
    help="Execution isolation mode.",
)
@click.option(
    "--unattended",
    is_flag=True,
    default=False,
    show_default=True,
    help="Run dispatch automation without interactive review gates.",
)
@click.option(
    "--yolo/--no-yolo",
    default=False,
    show_default=True,
    help="Enable composer yolo mode.",
)
@click.option("--target-branch", help="Target branch for the build.")
@click.option("--agent", "assigned_agent", help="Agent to assign to build work.")
@click.option(
    "--reset-expansion-output",
    is_flag=True,
    default=False,
    help="Delete existing generated expansion output before rebuilding a task ref.",
)
def build_command(
    input_ref: str | None,
    profile: str | None,
    stages: str | None,
    add_stage: tuple[str, ...],
    skip_stage: tuple[str, ...],
    stage_cap: tuple[str, ...],
    isolation: str,
    unattended: bool,
    yolo: bool,
    target_branch: str | None,
    assigned_agent: str | None,
    reset_expansion_output: bool,
) -> None:
    """Start lifecycle automation from a plan file or task reference."""
    if input_ref == "stop":
        _run_build_stop()
        return
    if input_ref == "resume":
        _run_build_resume()
        return
    if input_ref is None:
        invoke_build_skill()
        return

    opts = BuildOptions(
        profile=profile,
        skip_stages=_parse_skip_stages(skip_stage),
        isolation=cast(Isolation, isolation),
        unattended=unattended,
        composer_yolo=yolo,
        stages=_parse_stage_list(stages),
        add_stages=_parse_add_stage(add_stage),
        stage_caps=_parse_stage_cap(stage_cap),
        target_branch=target_branch,
        assigned_agent=assigned_agent,
        reset_expansion_output=reset_expansion_output,
    )
    project_id = resolve_project_id()
    db = _open_database()
    try:
        result = asyncio.run(build(input_ref, opts, db=db, project_id=project_id))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

    _echo_build_result(result)


@click.command("stop")
def build_stop_command() -> None:
    """Stop future dispatcher build ticks."""
    _run_build_stop()


@click.command("resume")
def build_resume_command() -> None:
    """Resume dispatcher build ticks."""
    _run_build_resume()


def _run_build_stop() -> None:
    project_id = resolve_project_id()
    db = _open_database()
    try:
        result = build_stop(db=db, project_id=project_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

    _echo_build_control_result(result)


def _run_build_resume() -> None:
    project_id = resolve_project_id()
    db = _open_database()
    try:
        result = build_resume(db=db, project_id=project_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

    _echo_build_control_result(result)
