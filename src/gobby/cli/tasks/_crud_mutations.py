"""Create, update, close, reopen, and delete task command implementations."""

from pathlib import Path
from typing import Any

import click

from gobby.cli.tasks._crud_services import CrudServices
from gobby.storage.tasks import TaskStaleStateError
from gobby.tasks.criteria_contract import TaskCriteriaError, require_validation_criteria
from gobby.tasks.state_semantics import is_task_closed
from gobby.tasks.validation import NO_WORK_CLOSE_REASONS


def create_task_impl(
    services: CrudServices,
    title: str,
    description: str | None,
    validation_criteria: str | None,
    priority: int,
    task_type: str,
    depends_on: tuple[str, ...],
    project_ref: str | None,
) -> None:
    from gobby.storage.projects import PERSONAL_PROJECT_ID

    project_id = services.resolve_project_ref(project_ref)
    if not project_id:
        project_id = PERSONAL_PROJECT_ID

    manager = services.get_task_manager()
    try:
        validation_criteria = require_validation_criteria(task_type, validation_criteria)
    except TaskCriteriaError as exc:
        raise click.ClickException(str(exc)) from exc
    resolved_blockers: list[tuple[str, Any]] = []
    dependency_failures: list[str] = []

    if depends_on:
        for blocker_ref in depends_on:
            blocker = services.resolve_task_id(manager, blocker_ref)
            if not blocker:
                dependency_failures.append(f"{blocker_ref}: task not found")
                continue
            resolved_blockers.append((blocker_ref, blocker))

    if dependency_failures:
        failure_lines = "\n".join(f"  {failure}" for failure in dependency_failures)
        raise click.ClickException(f"Could not add dependencies:\n{failure_lines}")

    try:
        task = manager.create_task(
            project_id=project_id,
            title=title,
            description=description,
            validation_criteria=validation_criteria,
            priority=priority,
            task_type=task_type,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
    project_ctx = services.get_project_context(cwd=Path.cwd())
    project_name = project_ctx.get("name") if project_ctx else None

    if project_name and task.seq_num:
        created_message = f"Created task {project_name}-#{task.seq_num}: {task.title}"
    else:
        created_message = f"Created task {task_ref}: {task.title}"

    dependency_messages: list[str] = []
    if resolved_blockers:
        from gobby.storage.task_dependencies import TaskDependencyManager

        dep_manager = TaskDependencyManager(manager.db)
        for blocker_ref, blocker in resolved_blockers:
            try:
                dep_manager.add_dependency(task.id, blocker.id, "blocks")
                blocker_display = f"#{blocker.seq_num}" if blocker.seq_num else blocker.id[:8]
                dependency_messages.append(f"  → depends on {blocker_display}")
            except Exception as e:
                dependency_failures.append(f"{blocker_ref}: {e}")

    if dependency_failures:
        try:
            manager.delete_task(task.id, unlink=True)
        except ValueError as e:
            dependency_failures.append(f"created task cleanup failed: {e}")
        failure_lines = "\n".join(f"  {failure}" for failure in dependency_failures)
        raise click.ClickException(f"Could not add dependencies:\n{failure_lines}")

    click.echo(created_message)
    for message in dependency_messages:
        click.echo(message)


def update_task_impl(
    services: CrudServices,
    task_id: str,
    title: str | None,
    validation_criteria: str | None,
    priority: int | None,
    parent_task_id: str | None,
    task_type: str | None,
    isolation: str | None,
    affected_files: list[str] | None,
) -> None:
    manager = services.get_task_manager()
    resolved = services.resolve_task_id(manager, task_id)
    if not resolved:
        return

    resolved_parent_id = None
    if parent_task_id:
        resolved_parent = services.resolve_task_id(manager, parent_task_id)
        if not resolved_parent:
            return
        resolved_parent_id = resolved_parent.id

    kwargs: dict[str, Any] = {}
    if title is not None:
        kwargs["title"] = title
    if validation_criteria is not None:
        kwargs["validation_criteria"] = validation_criteria
    if priority is not None:
        kwargs["priority"] = priority
    if resolved_parent_id is not None:
        kwargs["parent_task_id"] = resolved_parent_id
    if task_type is not None:
        kwargs["task_type"] = task_type
    if affected_files is not None:
        kwargs["affected_files"] = affected_files

    try:
        if isolation is not None:
            kwargs["isolation"] = services.validate_task_isolation_artifacts(
                manager, resolved.id, isolation
            )
        task = manager.update_task(resolved.id, **kwargs)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
    click.echo(f"Updated task {task_ref}")


def close_task_impl(
    services: CrudServices,
    task_ids: tuple[str, ...],
    reason: str,
) -> None:
    manager = services.get_task_manager()

    expanded_ids = services.parse_task_refs(task_ids)

    closed_count = 0
    failed_count = 0

    for task_id in expanded_ids:
        resolved = services.resolve_task_id(manager, task_id)
        if not resolved:
            failed_count += 1
            continue

        children = manager.list_tasks(parent_task_id=resolved.id, limit=1000)
        task_ref = f"#{resolved.seq_num}" if resolved.seq_num else resolved.id[:8]
        if resolved.task_type != "epic" and not children and reason not in NO_WORK_CLOSE_REASONS:
            click.echo(
                f"Cannot close {task_ref} through the direct CLI: non-epic leaves require "
                "the criterion-to-evidence close_task contract.",
                err=True,
            )
            failed_count += 1
            continue

        if children:
            open_children = [c for c in children if not is_task_closed(c)]
            if open_children:
                click.echo(
                    f"Cannot close {task_ref}: {len(open_children)} child tasks still open",
                    err=True,
                )
                failed_count += 1
                continue

        closed_ancestors: list[str] = []
        try:
            task = manager.close_task(resolved.id, reason=reason, closed_ancestors=closed_ancestors)
        except TaskStaleStateError:
            click.echo(f"Cannot close {task_ref}: task is already closed", err=True)
            failed_count += 1
            continue

        task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]
        click.echo(f"Closed task {task_ref} ({reason})")
        for ancestor_id in closed_ancestors:
            ancestor = manager.get_task(ancestor_id)
            if ancestor is None:
                continue
            ancestor_ref = f"#{ancestor.seq_num}" if ancestor.seq_num else ancestor.id[:8]
            click.echo(f"Auto-closed parent {ancestor_ref} ({reason})")
        closed_count += 1

    if len(expanded_ids) > 1:
        if failed_count > 0:
            click.echo(f"\nClosed {closed_count}/{len(expanded_ids)} tasks ({failed_count} failed)")
        else:
            click.echo(f"\nClosed {closed_count} tasks")

    if failed_count > 0:
        raise SystemExit(1)


def reopen_task_impl(services: CrudServices, task_id: str, reason: str | None) -> None:
    manager = services.get_task_manager()
    resolved = services.resolve_task_id(manager, task_id)
    if not resolved:
        return

    resolved_ref = f"#{resolved.seq_num}" if resolved.seq_num else resolved.id[:8]

    if not is_task_closed(resolved) and not resolved.is_escalated:
        click.echo(
            f"Task {resolved_ref} is already active",
            err=True,
        )
        return

    task = manager.reopen_task(resolved.id, reason=reason)

    task_ref = f"#{task.seq_num}" if task.seq_num else task.id[:8]

    if reason:
        click.echo(f"Reopened task {task_ref} ({reason})")
    else:
        click.echo(f"Reopened task {task_ref}")


def delete_task_impl(
    services: CrudServices, task_refs: tuple[str, ...], cascade: bool, unlink: bool, yes: bool
) -> None:
    manager = services.get_task_manager()

    all_refs = services.parse_task_refs(task_refs)
    resolved_tasks = []
    for ref in all_refs:
        resolved = services.resolve_task_id(manager, ref)
        if resolved:
            resolved_tasks.append((ref, resolved))

    if not resolved_tasks:
        raise SystemExit(1)

    if not yes:
        task_list = ", ".join(ref for ref, _ in resolved_tasks)
        if not click.confirm(f"Delete {len(resolved_tasks)} task(s): {task_list}?"):
            click.echo("Cancelled.")
            return

    deleted = 0
    failed = 0
    for ref, resolved in resolved_tasks:
        try:
            manager.delete_task(resolved.id, cascade=cascade, unlink=unlink)
            click.echo(f"Deleted task {resolved.id}")
            deleted += 1
        except ValueError as e:
            msg = str(e)
            if "has children" in msg:
                msg = f"Task {ref} has children. Use --cascade to delete with all subtasks."
            elif "dependent task(s)" in msg:
                msg = (
                    f"Task {ref} has dependent tasks. "
                    f"Use --cascade to delete them, or --unlink to preserve them."
                )
            click.echo(f"Error: {msg}", err=True)
            failed += 1

    if len(resolved_tasks) > 1:
        click.echo(f"\nDeleted {deleted}/{len(resolved_tasks)} tasks.")

    if failed > 0:
        raise SystemExit(1)


def de_escalate_impl(
    services: CrudServices,
    task_id: str,
    reason: str,
    reset_validation: bool,
    reset_stage_attempts: bool,
    restore_stage_from_history: bool,
) -> None:
    manager = services.get_task_manager()
    resolved = services.resolve_task_id(manager, task_id)
    if not resolved:
        return

    if not resolved.is_escalated:
        click.echo(
            f"Task {resolved.id[:8]} is not escalated",
            err=True,
        )
        return

    manager.de_escalate_task(
        resolved.id,
        reason=reason,
        reset_validation=reset_validation,
        reset_stage_attempts=reset_stage_attempts,
        restore_stage_from_history=restore_stage_from_history,
    )
    click.echo(f"De-escalated task {resolved.id[:8]} ({reason})")
    if reset_validation:
        click.echo("  Validation fail count reset to 0")
    if reset_stage_attempts:
        click.echo("  Current stage work attempts reset to 0")
    if restore_stage_from_history:
        click.echo("  Current stage restored from lifecycle history")
