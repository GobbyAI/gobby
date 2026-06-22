"""CRUD command registrations for task management."""

import click

from gobby.cli.tasks._crud_common import (
    ISOLATION_CHOICE,
    TASK_TYPE_CHOICE,
)
from gobby.cli.tasks._crud_common import (
    current_stage_display as _current_stage_display,
)
from gobby.cli.tasks._crud_detail import show_task_impl, validation_history_impl
from gobby.cli.tasks._crud_listing import (
    blocked_tasks_impl,
    list_tasks_impl,
    ready_tasks_impl,
    task_stats_impl,
)
from gobby.cli.tasks._crud_mutations import (
    close_task_impl,
    create_task_impl,
    de_escalate_impl,
    delete_task_impl,
    reopen_task_impl,
    update_task_impl,
)
from gobby.cli.tasks._crud_services import CrudServices
from gobby.cli.tasks._stage_filters import STAGE_STATE_CHOICE, filter_tasks_by_stage
from gobby.cli.tasks._utils import (
    collect_ancestors,
    compute_tree_prefixes,
    format_task_list,
    get_claimed_task_owners,
    get_task_manager,
    parse_task_refs,
    resolve_task_id,
    sort_tasks_for_tree,
)
from gobby.cli.utils import resolve_project_ref
from gobby.tasks.isolation import validate_task_isolation_artifacts
from gobby.utils.project_context import get_project_context


def _services() -> CrudServices:
    return CrudServices(
        get_task_manager=get_task_manager,
        resolve_project_ref=resolve_project_ref,
        filter_tasks_by_stage=filter_tasks_by_stage,
        collect_ancestors=collect_ancestors,
        sort_tasks_for_tree=sort_tasks_for_tree,
        compute_tree_prefixes=compute_tree_prefixes,
        get_claimed_task_owners=get_claimed_task_owners,
        format_task_list=format_task_list,
        parse_task_refs=parse_task_refs,
        resolve_task_id=resolve_task_id,
        get_project_context=get_project_context,
        validate_task_isolation_artifacts=validate_task_isolation_artifacts,
    )


@click.command("list")
@click.option(
    "--active",
    is_flag=True,
    help="Show all non-closed work",
)
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--stage", "stage_name", help="Filter by exact stage name")
@click.option("--state", "stage_state", type=STAGE_STATE_CHOICE, help="Filter by stage state")
@click.option("--claimed", is_flag=True, help="Show only claimed tasks")
@click.option("--unclaimed", is_flag=True, help="Show only unclaimed tasks")
@click.option(
    "--ready",
    is_flag=True,
    help="Show only ready tasks with no unresolved blocking dependencies",
)
@click.option("--blocked", is_flag=True, help="Show only canonically blocked tasks")
@click.option("--closed", "closed_only", is_flag=True, help="Show only canonically closed tasks")
@click.option("--escalated", is_flag=True, help="Show only canonically escalated tasks")
@click.option("--limit", "-l", default=50, help="Max tasks to show")
@click.option(
    "--group",
    "group_by",
    type=click.Choice(["project", "stage"]),
    default=None,
    help=(
        "Group output by project or stage. When omitted, tasks are "
        "auto-grouped by project if no project context is detected."
    ),
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_tasks(
    active: bool,
    project_ref: str | None,
    stage_name: str | None,
    stage_state: str | None,
    claimed: bool,
    unclaimed: bool,
    ready: bool,
    blocked: bool,
    closed_only: bool,
    escalated: bool,
    limit: int,
    group_by: str | None,
    json_format: bool,
) -> None:
    """List tasks."""
    list_tasks_impl(
        _services(),
        active,
        project_ref,
        stage_name,
        stage_state,
        claimed,
        unclaimed,
        ready,
        blocked,
        closed_only,
        escalated,
        limit,
        group_by,
        json_format,
    )


@click.command("ready")
@click.option("--limit", "-n", default=10, help="Max results")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--priority", type=int, help="Filter by priority")
@click.option("--type", "-t", "task_type", help="Filter by type")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
@click.option("--flat", is_flag=True, help="Flat list without tree hierarchy")
def ready_tasks(
    limit: int,
    project_ref: str | None,
    priority: int | None,
    task_type: str | None,
    json_format: bool,
    flat: bool,
) -> None:
    """List tasks with no unresolved blocking dependencies."""
    ready_tasks_impl(_services(), limit, project_ref, priority, task_type, json_format, flat)


@click.command("blocked")
@click.option("--limit", "-n", default=20, help="Max results")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def blocked_tasks(limit: int, project_ref: str | None, json_format: bool) -> None:
    """List blocked tasks with what blocks them."""
    blocked_tasks_impl(_services(), limit, project_ref, json_format)


@click.command("stats")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def task_stats(project_ref: str | None, json_format: bool) -> None:
    """Show task statistics."""
    task_stats_impl(_services(), project_ref, json_format)


@click.command("create")
@click.argument("title")
@click.option("--description", "-d", help="Task description")
@click.option("--priority", "-p", type=int, default=2, help="Priority (1=High, 2=Med, 3=Low)")
@click.option("--type", "-t", "task_type", type=TASK_TYPE_CHOICE, default="task", help="Task type")
@click.option("--depends-on", "-D", multiple=True, help="Task(s) this task depends on (#N, UUID)")
@click.option("--project", "project_ref", help="Target project (name or UUID)")
def create_task(
    title: str,
    description: str | None,
    priority: int,
    task_type: str,
    depends_on: tuple[str, ...],
    project_ref: str | None,
) -> None:
    """Create a new task.

    Examples:
        gobby tasks create "Fix bug"
        gobby tasks create "Implement feature" --depends-on "#1"
        gobby tasks create "Final review" -D "#1" -D "#2"
        gobby tasks create "Note" --project _personal
    """
    create_task_impl(_services(), title, description, priority, task_type, depends_on, project_ref)


@click.command("show")
@click.argument("task_id", metavar="TASK")
def show_task(task_id: str) -> None:
    """Show details for a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    show_task_impl(_services(), task_id)


@click.command("update")
@click.argument("task_id", metavar="TASK")
@click.option("--title", "-T", help="New title")
@click.option("--priority", type=int, help="New priority")
@click.option("--parent", "parent_task_id", help="Parent task (#N, path, or UUID)")
@click.option(
    "--task-type",
    "task_type",
    type=TASK_TYPE_CHOICE,
    help="New task type",
)
@click.option(
    "--isolation",
    type=ISOLATION_CHOICE,
    help="New automation isolation mode",
)
def update_task(
    task_id: str,
    title: str | None,
    priority: int | None,
    parent_task_id: str | None,
    task_type: str | None,
    isolation: str | None,
) -> None:
    """Update a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    update_task_impl(_services(), task_id, title, priority, parent_task_id, task_type, isolation)


@click.command("close")
@click.argument("task_ids", metavar="TASK", nargs=-1, required=True)
@click.option("--reason", "-r", default="completed", help="Reason for closing")
@click.option("--skip-validation", is_flag=True, help="Skip validation checks")
@click.option("--force", "-f", is_flag=True, help="Alias for --skip-validation")
def close_task_cmd(
    task_ids: tuple[str, ...], reason: str, skip_validation: bool, force: bool
) -> None:
    """Close one or more tasks.

    TASK can be: #N (e.g., #1, #47), seq_num (e.g., 47), path (e.g., 1.2.3), or UUID.
    Multiple tasks can be specified separated by spaces or commas.

    Examples:
        gobby tasks close #42
        gobby tasks close 42 43 44
        gobby tasks close abc123,#45,46

    Parent tasks require all children to be closed first.
    Use --skip-validation or --force for wont_fix, duplicate, etc.
    """
    close_task_impl(_services(), task_ids, reason, skip_validation, force)


@click.command("reopen")
@click.argument("task_id", metavar="TASK")
@click.option("--reason", "-r", default=None, help="Reason for reopening")
def reopen_task_cmd(task_id: str, reason: str | None) -> None:
    """Reopen a task to active stage state.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.

    Works on closed or escalated tasks. Clears ownership, closure/escalation
    fields, and resets validation_fail_count.
    """
    reopen_task_impl(_services(), task_id, reason)


@click.command("delete")
@click.argument("task_refs", nargs=-1, required=True, metavar="TASKS...")
@click.option("--cascade", "-c", is_flag=True, help="Delete child tasks and dependent tasks")
@click.option(
    "--unlink", "-u", is_flag=True, help="Remove dependency links but preserve dependent tasks"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def delete_task(task_refs: tuple[str, ...], cascade: bool, unlink: bool, yes: bool) -> None:
    """Delete one or more tasks.

    TASKS can be: #N (e.g., #1, #47), comma-separated (#1,#2,#3), or UUIDs.
    Multiple tasks can be specified separated by spaces or commas.

    Examples:
        gobby tasks delete #42
        gobby tasks delete #42,#43,#44 --cascade
        gobby tasks delete #42 #43 #44 --yes
        gobby tasks delete #42 --unlink
    """
    delete_task_impl(_services(), task_refs, cascade, unlink, yes)


@click.command("de-escalate")
@click.argument("task_id", metavar="TASK")
@click.option("--reason", "-r", required=True, help="Reason for de-escalation")
@click.option("--reset-validation", is_flag=True, help="Reset validation fail count")
@click.option("--reset-stage-attempts", is_flag=True, help="Reset current stage work attempts")
@click.option(
    "--restore-stage-from-history",
    is_flag=True,
    help="Restore a current ready stage from prior build_stop review_approved history",
)
def de_escalate_cmd(
    task_id: str,
    reason: str,
    reset_validation: bool,
    reset_stage_attempts: bool,
    restore_stage_from_history: bool,
) -> None:
    """Return an escalated task to its preserved current stage.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.

    Use after human intervention resolves the issue that caused escalation.
    """
    de_escalate_impl(
        _services(),
        task_id,
        reason,
        reset_validation,
        reset_stage_attempts,
        restore_stage_from_history,
    )


@click.command("validation-history")
@click.argument("task_id", metavar="TASK")
@click.option("--clear", is_flag=True, help="Clear validation history")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def validation_history_cmd(task_id: str, clear: bool, json_format: bool) -> None:
    """View or clear validation history for a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    validation_history_impl(_services(), task_id, clear, json_format)


__all__ = [
    "blocked_tasks",
    "close_task_cmd",
    "create_task",
    "de_escalate_cmd",
    "delete_task",
    "list_tasks",
    "ready_tasks",
    "reopen_task_cmd",
    "show_task",
    "task_stats",
    "update_task",
    "validation_history_cmd",
    "_current_stage_display",
]
