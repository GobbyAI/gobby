"""Task-reference resolution helpers for the task CLI."""

from pathlib import Path

import click

from gobby.storage.tasks import LocalTaskManager, Task, TaskNotFoundError
from gobby.utils.project_context import get_project_context


def resolve_task_id(
    manager: LocalTaskManager, task_id: str, project_id: str | None = None
) -> Task | None:
    """Resolve a task ID to a Task with user-friendly errors.

    Supports multiple reference formats:
      - #N: Project-scoped seq_num (e.g., #1, #47) - requires project_id
      - 1.2.3: Path cache format - requires project_id
      - UUID: Direct UUID lookup
      - Prefix: ID prefix matching for partial UUIDs

    Args:
        manager: The task manager
        task_id: Task reference in any supported format
        project_id: Project ID for scoped lookups (#N and path formats).
                   If not provided, will try to get from project context.

    Returns:
        The resolved Task, or None if not found (with error message printed)
    """
    # Get project_id from context if not provided
    if project_id is None:
        ctx = get_project_context(cwd=Path.cwd())
        project_id = ctx.get("id") if ctx else None

    # Try #N format, numeric format (treated as #N), or path format (requires project_id)
    if project_id and (task_id.startswith("#") or task_id.isdigit() or _is_path_format(task_id)):
        # Auto-prefix numeric IDs with #
        if task_id.isdigit():
            task_id = f"#{task_id}"

        try:
            resolved_uuid = manager.resolve_task_reference(task_id, project_id)
            return manager.get_task(resolved_uuid)
        except TaskNotFoundError as e:
            click.echo(f"Task '{task_id}' not found: {e}", err=True)
            return None
        except ValueError as e:
            # Deprecation or format errors
            click.echo(f"Error: {e}", err=True)
            return None

    # Try exact UUID match
    try:
        return manager.get_task(task_id)
    except ValueError:
        pass

    # Try prefix matching for partial UUIDs
    matches = manager.find_tasks_by_prefix(task_id)

    if len(matches) == 0:
        click.echo(f"Task '{task_id}' not found", err=True)
        return None
    elif len(matches) == 1:
        return matches[0]
    else:
        click.echo(f"Ambiguous task ID '{task_id}' matches {len(matches)} tasks:", err=True)
        for task in matches[:5]:
            click.echo(f"  {task.id}: {task.title}", err=True)
        if len(matches) > 5:
            click.echo(f"  ... and {len(matches) - 5} more", err=True)
        return None


def _is_path_format(ref: str) -> bool:
    """Check if a reference is in path format (e.g., 1.2.3)."""
    if "." not in ref:
        return False
    parts = ref.split(".")
    return all(part.isdigit() for part in parts)


def parse_task_refs(refs: tuple[str, ...]) -> list[str]:
    """Parse task references from various CLI input formats.

    Handles multiple input formats commonly used in CLI:
    - Single reference: "42", "#42", "abc123-def"
    - Comma-separated: "#42,#43,#44" or "42,43,44"
    - Space-separated: passed as tuple from Click variadic args
    - Mixed: "#42,#43 #44" with both separators

    Numeric references are normalized to #N format.
    UUID-like references are passed through unchanged.

    Args:
        refs: Tuple of reference strings from Click variadic argument

    Returns:
        List of normalized task references
    """
    result: list[str] = []

    for arg in refs:
        # Split on commas first
        parts = arg.split(",")
        for part in parts:
            ref = part.strip()
            if not ref:
                continue

            # Normalize pure numeric to #N format
            if ref.isdigit():
                ref = f"#{ref}"

            result.append(ref)

    return result
