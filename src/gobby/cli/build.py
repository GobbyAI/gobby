"""CLI surface for build lifecycle automation."""

from __future__ import annotations

import asyncio
from typing import cast

import click

from gobby.build import BuildOptions, BuildResult, build
from gobby.config.build import Isolation
from gobby.storage.database import LocalDatabase

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


def _parse_skip_stages(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [stage.strip() for stage in raw.split(",") if stage.strip()]


def _echo_build_result(result: BuildResult) -> None:
    click.echo(f"Task: {result.task_id}")
    click.echo(f"Lifecycle: {result.initial_lifecycle}")
    if result.applied_stages_skipped:
        click.echo(f"Skipped stages: {', '.join(result.applied_stages_skipped)}")
    click.echo(f"Dispatcher tick: {result.tick_dispatched}")


@click.command("build")
@click.argument("input_ref", required=False, metavar="[INPUT]")
@click.option("--profile", help="Build profile to apply.")
@click.option("--skip-stage", help="Comma-separated lifecycle stages to skip.")
@click.option(
    "--isolation",
    type=click.Choice(["none", "worktree", "clone"]),
    default="worktree",
    show_default=True,
    help="Execution isolation mode.",
)
@click.option(
    "--yolo/--no-yolo",
    default=False,
    show_default=True,
    help="Enable composer yolo mode.",
)
@click.option(
    "--max-review-rounds",
    default=3,
    show_default=True,
    type=int,
    help="Maximum review rounds before stopping.",
)
@click.option("--target-branch", help="Target branch for the build.")
@click.option("--max-expansion-attempts", type=int, help="Expansion retry cap.")
@click.option("--max-qa-rounds", type=int, help="Per-leaf QA retry cap.")
@click.option("--max-merge-attempts", type=int, help="Merge retry cap.")
@click.option("--max-holistic-rounds", type=int, help="Holistic-review retry cap.")
@click.option("--agent", "assigned_agent", help="Agent to assign to build work.")
def build_command(
    input_ref: str | None,
    profile: str | None,
    skip_stage: str | None,
    isolation: str,
    yolo: bool,
    max_review_rounds: int,
    target_branch: str | None,
    max_expansion_attempts: int | None,
    max_qa_rounds: int | None,
    max_merge_attempts: int | None,
    max_holistic_rounds: int | None,
    assigned_agent: str | None,
) -> None:
    """Start lifecycle automation from a plan file or task reference."""
    if input_ref is None:
        invoke_build_skill()
        return

    opts = BuildOptions(
        profile=profile,
        skip_stages=_parse_skip_stages(skip_stage),
        isolation=cast(Isolation, isolation),
        unattended=False,
        composer_yolo=yolo,
        max_review_rounds=max_review_rounds,
        max_expansion_attempts=max_expansion_attempts,
        max_qa_rounds=max_qa_rounds,
        max_merge_attempts=max_merge_attempts,
        max_holistic_rounds=max_holistic_rounds,
        target_branch=target_branch,
        assigned_agent=assigned_agent,
    )
    project_id = resolve_project_id()
    db = LocalDatabase()
    try:
        result = asyncio.run(build(input_ref, opts, db=db, project_id=project_id))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        db.close()

    _echo_build_result(result)
