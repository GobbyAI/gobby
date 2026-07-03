"""Task detail and validation-history command implementations."""

import click

from gobby.cli.tasks._crud_common import current_stage_display
from gobby.cli.tasks._crud_services import CrudServices
from gobby.tasks.state_semantics import serialize_task_state
from gobby.utils.json_helpers import json_dumps


def show_task_impl(services: CrudServices, task_id: str) -> None:
    manager = services.get_task_manager()
    task = services.resolve_task_id(manager, task_id)

    if not task:
        return

    blocker_ids = sorted(task.active_blocked_by)
    state = serialize_task_state(task)
    if state["is_closed"]:
        stage_display = "closed"
    elif state["is_escalated"]:
        stage_display = "escalated"
    else:
        stage_display = current_stage_display(state)

    click.echo(f"Task: {task.title}")
    click.echo(f"ID: {task.id}")
    if task.seq_num:
        click.echo(f"Ref: #{task.seq_num}")
    click.echo(f"Current Stage: {stage_display}")
    click.echo(f"Owner Session: {state['owner_session_id'] or '-'}")
    click.echo(f"Blocked: {'yes' if state['is_blocked'] else 'no'}")
    click.echo(f"Escalated: {'yes' if state['is_escalated'] else 'no'}")
    click.echo(f"Merge Ready: {'yes' if state['is_merge_ready'] else 'no'}")
    click.echo(f"Closed: {'yes' if state['is_closed'] else 'no'}")
    click.echo(f"Priority: {task.priority}")
    click.echo(f"Type: {task.task_type}")
    click.echo(f"Created: {task.created_at}")
    click.echo(f"Updated: {task.updated_at}")
    if state["closed_at"]:
        click.echo(f"Closed At: {state['closed_at']}")
    if state["closed_reason"]:
        click.echo(f"Closed Reason: {state['closed_reason']}")
    if state["closed_in_session_id"]:
        click.echo(f"Closed In Session: {state['closed_in_session_id']}")
    if state["closed_commit_sha"]:
        click.echo(f"Closed Commit: {state['closed_commit_sha']}")
    if state["escalated_at"]:
        click.echo(f"Escalated At: {state['escalated_at']}")
    if state["escalation_reason"]:
        click.echo(f"Escalation Reason: {state['escalation_reason']}")
    if task.labels:
        click.echo(f"Labels: {', '.join(task.labels)}")
    if blocker_ids:
        blocker_refs: list[str] = []
        for blocker_id in blocker_ids:
            try:
                blocker_task = manager.get_task(blocker_id)
                blocker_refs.append(
                    f"#{blocker_task.seq_num}" if blocker_task.seq_num else blocker_task.id[:8]
                )
            except Exception:
                blocker_refs.append(blocker_id[:8])
        click.echo(f"Blocked By: {', '.join(blocker_refs)}")
    if task.description:
        click.echo(f"\n{task.description}")


def validation_history_impl(
    services: CrudServices, task_id: str, clear: bool, json_format: bool
) -> None:
    from gobby.tasks.validation_history import ValidationHistoryManager

    manager = services.get_task_manager()
    resolved = services.resolve_task_id(manager, task_id)
    if not resolved:
        return

    history_manager = ValidationHistoryManager(manager.db)

    if clear:
        history_manager.clear_history(resolved.id)
        manager.update_task(resolved.id, validation_fail_count=0)
        click.echo(f"Cleared validation history for {resolved.id[:8]}")
        return

    iterations = history_manager.get_iteration_history(resolved.id)

    if json_format:
        result = {
            "task_id": resolved.id,
            "iterations": [
                {
                    "iteration": it.iteration,
                    "status": it.status,
                    "feedback": it.feedback,
                    "issues": [i.to_dict() for i in (it.issues or [])],
                    "created_at": it.created_at,
                }
                for it in iterations
            ],
        }
        click.echo(json_dumps(result, indent=2, default=str))
        return

    if not iterations:
        click.echo(f"No validation history for task {resolved.id[:8]}")
        return

    click.echo(f"Validation history for {resolved.id[:8]}:")
    for it in iterations:
        click.echo(f"\n  Iteration {it.iteration}: {it.status}")
        if it.feedback:
            feedback_preview = it.feedback[:100] + "..." if len(it.feedback) > 100 else it.feedback
            click.echo(f"    Feedback: {feedback_preview}")
        if it.issues:
            click.echo(f"    Issues: {len(it.issues)}")
        click.echo(f"    Created: {it.created_at}")
