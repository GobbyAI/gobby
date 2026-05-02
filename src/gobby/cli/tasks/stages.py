"""Stage-manifest task commands."""

from __future__ import annotations

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.storage.tasks import IllegalStageTransitionError, StageState


def _transition_error(error: IllegalStageTransitionError) -> click.ClickException:
    return click.ClickException(
        "Illegal stage transition: "
        f"{error.stage_name} is {error.current_state}; "
        f"cannot {error.attempted_transition} with policy {error.review_policy}"
    )


def _render_stage_table(rows: list[StageState]) -> str:
    headers = ("Stage", "State", "Policy", "Work", "Review", "Updated")
    body = [
        (
            row.stage_name,
            row.state,
            row.review_policy,
            str(row.work_attempt_count),
            str(row.review_round_count),
            (row.updated_at or "-")[:10],
        )
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(item[index]) for item in body)) if body else len(header)
        for index, header in enumerate(headers)
    ]
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(item[index].ljust(widths[index]) for index in range(len(headers)))
        for item in body
    )
    return "\n".join(lines)


@click.command("stages")
@click.argument("task_ref", metavar="TASK")
def stages_cmd(task_ref: str) -> None:
    """Render the stage manifest for a task."""

    manager = get_task_manager()
    task = resolve_task_id(manager, task_ref)
    if task is None:
        return
    rows = manager.stage_states.list_for_task(task.id)
    click.echo(f"#{task.seq_num or task.id[:8]}  {task.title}")
    if not rows:
        click.echo("No stage manifest found.")
        return
    click.echo(_render_stage_table(rows))


@click.command("advance")
@click.argument("task_ref", metavar="TASK")
@click.option("--stage", "stage_name", help="Stage name; must match the current stage.")
def advance_cmd(task_ref: str, stage_name: str | None) -> None:
    """Complete or start the current stage according to review policy."""

    manager = get_task_manager()
    task = resolve_task_id(manager, task_ref)
    if task is None:
        return
    current = manager.stage_states.current_stage(task.id)
    if current is None:
        click.echo("Task stage manifest is exhausted.")
        return
    if stage_name is not None and stage_name != current.stage_name:
        raise click.ClickException(
            f"--stage must match current stage {current.stage_name}; got {stage_name}"
        )

    try:
        if current.state == "ready":
            advanced = manager.stage_states.start_stage(
                task.id, current.stage_name, by_session_id=None
            )
        elif current.review_policy == "required" and current.state == "in_progress":
            raise click.ClickException(
                f"{current.stage_name} requires review; run gobby tasks review --submit first"
            )
        else:
            advanced = manager.stage_states.complete_stage(
                task.id, current.stage_name, by_session_id=None
            )
            next_stage = manager.stage_states.current_stage(task.id)
            if next_stage is not None and next_stage.state == "ready":
                manager.stage_states.start_stage(task.id, next_stage.stage_name, by_session_id=None)
    except IllegalStageTransitionError as exc:
        raise _transition_error(exc) from exc

    click.echo(f"{advanced.stage_name}: {advanced.state}")


__all__ = ["advance_cmd", "stages_cmd"]
