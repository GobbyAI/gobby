"""
Label management commands for tasks.
"""

import click

from gobby.cli.tasks._utils import get_task_manager, resolve_task_id


@click.group("label")
def label_cmd() -> None:
    """Manage task labels."""
    pass


@label_cmd.command("add")
@click.argument("task_id", metavar="TASK")
@click.argument("label")
def add_label(task_id: str, label: str) -> None:
    """Add a label to a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        click.secho(f"Error: Could not resolve task '{task_id}'", fg="red", err=True)
        raise SystemExit(1)

    try:
        manager.add_label(resolved.id, label)
    except ValueError as e:
        click.secho(f"Error: {e}", fg="red", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.secho(f"Unexpected error adding label: {e}", fg="red", err=True)
        raise SystemExit(1) from None
    click.echo(f"Added label '{label}' to task {resolved.id}")


@label_cmd.command("remove")
@click.argument("task_id", metavar="TASK")
@click.argument("label")
def remove_label(task_id: str, label: str) -> None:
    """Remove a label from a task.

    TASK can be: #N (e.g., #1, #47), path (e.g., 1.2.3), or UUID.
    """
    manager = get_task_manager()
    resolved = resolve_task_id(manager, task_id)
    if not resolved:
        click.secho(f"Error: Could not resolve task '{task_id}'", fg="red", err=True)
        raise SystemExit(1)

    try:
        manager.remove_label(resolved.id, label)
    except ValueError as e:
        click.secho(f"Error: {e}", fg="red", err=True)
        raise SystemExit(1) from None
    except Exception as e:
        click.secho(f"Unexpected error removing label: {e}", fg="red", err=True)
        raise SystemExit(1) from None
    click.echo(f"Removed label '{label}' from task {resolved.id}")
