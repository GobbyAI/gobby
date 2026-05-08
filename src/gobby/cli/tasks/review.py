"""Stage-axis review task commands."""

from __future__ import annotations

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.storage.tasks import IllegalStageTransitionError


def _format_transition_error(error: IllegalStageTransitionError) -> str:
    return (
        f"{error.stage_name} is {error.current_state}; "
        f"cannot {error.attempted_transition} with policy {error.review_policy}"
    )


@click.command("review")
@click.argument("task_ref", metavar="TASK")
@click.option("--submit", "action_submit", is_flag=True, help="Submit current stage for review.")
@click.option("--approve", "action_approve", is_flag=True, help="Approve current stage review.")
@click.option("--reject", "action_reject", is_flag=True, help="Reject current stage review.")
@click.option("--reason", help="Reason for --reject.")
def review_cmd(
    task_ref: str,
    action_submit: bool,
    action_approve: bool,
    action_reject: bool,
    reason: str | None,
) -> None:
    """Run review transitions on the current stage."""

    selected = [action_submit, action_approve, action_reject]
    if sum(1 for item in selected if item) != 1:
        raise click.ClickException("Choose exactly one of --submit, --approve, or --reject")
    if action_reject and not reason:
        raise click.ClickException("--reject requires --reason")

    manager = get_task_manager()
    task = resolve_task_id(manager, task_ref)
    if task is None:
        return
    current = manager.stage_states.current_stage(task.id)
    if current is None:
        raise click.ClickException("Task stage manifest is exhausted")

    try:
        if action_submit:
            updated = manager.stage_states.submit_for_review(
                task.id, current.stage_name, by_session_id=None
            )
        elif action_approve:
            updated = manager.stage_states.approve_review(
                task.id, current.stage_name, by_session_id=None
            )
        else:
            assert reason is not None
            updated = manager.stage_states.reject_review(
                task.id, current.stage_name, reason=reason, by_session_id=None
            )
    except IllegalStageTransitionError as exc:
        raise click.ClickException(_format_transition_error(exc)) from exc

    click.echo(f"{updated.stage_name}: {updated.state}")


__all__ = ["review_cmd"]
