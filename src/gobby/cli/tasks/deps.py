"""
Dependency management commands for tasks.
"""

from typing import Literal

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id
from gobby.tasks.state_semantics import projected_task_state


@click.group("dep")
def dep_cmd() -> None:
    """Manage task dependencies."""
    pass


DependencyType = Literal["blocks", "related", "discovered-from"]


@dep_cmd.command("add")
@click.argument("task_id", metavar="TASK")
@click.argument("blocker_id", metavar="BLOCKER")
@click.option(
    "--type",
    "-t",
    "dep_type",
    default="blocks",
    help="Dependency type (blocks, related, discovered-from)",
)
def dep_add(task_id: str, blocker_id: str, dep_type: DependencyType) -> None:
    """Add a dependency: BLOCKER blocks TASK.

    TASK/BLOCKER can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.

    Example: gobby tasks dep add #3 #1
    means #1 blocks #3 (task #3 depends on task #1)
    """
    from gobby.storage.task_dependencies import TaskDependencyManager

    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    blocker = resolve_task_id(manager, blocker_id)
    if not blocker:
        return

    dep_manager = TaskDependencyManager(manager.db)
    try:
        dep_manager.add_dependency(resolved.id, blocker.id, dep_type)
        click.echo(f"Added dependency: {blocker.id[:8]} {dep_type} {resolved.id[:8]}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)


@dep_cmd.command("remove")
@click.argument("task_id", metavar="TASK")
@click.argument("blocker_id", metavar="BLOCKER")
def dep_remove(task_id: str, blocker_id: str) -> None:
    """Remove a dependency between tasks.

    TASK/BLOCKER can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    from gobby.storage.task_dependencies import TaskDependencyManager

    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    blocker = resolve_task_id(manager, blocker_id)
    if not blocker:
        return

    dep_manager = TaskDependencyManager(manager.db)
    dep_manager.remove_dependency(resolved.id, blocker.id)
    click.echo(f"Removed dependency between {resolved.id[:8]} and {blocker.id[:8]}")


@dep_cmd.command("tree")
@click.argument("task_id", metavar="TASK")
def dep_tree(task_id: str) -> None:
    """Show dependency tree for a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    from gobby.storage.task_dependencies import TaskDependencyManager

    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        return

    dep_manager = TaskDependencyManager(manager.db)
    tree = dep_manager.get_dependency_tree(resolved.id)

    click.echo(f"Dependency tree for {resolved.id[:8]} ({resolved.title}):")
    click.echo("")

    # Show blockers (what this task depends on)
    if tree.get("blockers"):
        click.echo("Blocked by:")
        for b in tree["blockers"]:
            status_icon = "✓" if projected_task_state(b) == "closed" else "○"
            click.echo(f"  {status_icon} {b['id'][:8]}: {b.get('title', 'Unknown')}")
    else:
        click.echo("Blocked by: (none)")

    # Show blocking (what depends on this task)
    if tree.get("blocking"):
        click.echo("\nBlocking:")
        for b in tree["blocking"]:
            status_icon = "✓" if projected_task_state(b) == "closed" else "○"
            click.echo(f"  {status_icon} {b['id'][:8]}: {b.get('title', 'Unknown')}")
    else:
        click.echo("\nBlocking: (none)")


@dep_cmd.command("cycles")
def dep_cycles() -> None:
    """Check for dependency cycles."""
    from gobby.storage.task_dependencies import TaskDependencyManager

    manager = get_task_manager()
    dep_manager = TaskDependencyManager(manager.db)
    cycles = dep_manager.check_cycles()

    if cycles:
        click.echo(f"Found {len(cycles)} dependency cycles:", err=True)
        for cycle in cycles:
            click.echo(f"  {' -> '.join(c[:8] for c in cycle)}", err=True)
    else:
        click.echo("✓ No dependency cycles found")
