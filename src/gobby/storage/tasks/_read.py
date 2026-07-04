"""Task lookup and hydration helpers."""

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._blocking import hydrate_task_blocking_state
from gobby.storage.tasks._id import resolve_task_reference
from gobby.storage.tasks._models import Task, TaskNotFoundError
from gobby.storage.tasks._stage_hydration import hydrate_task_stage_state
from gobby.utils.uuid_validation import parse_uuid_reference


def _escape_like_prefix(prefix: str) -> str:
    """Escape SQL LIKE wildcard characters in an ID prefix."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _is_task_uuid(value: str) -> bool:
    return parse_uuid_reference(value) is not None


def get_task(db: HubDatabase, task_id: str, project_id: str | None = None) -> Task:
    """Get a task by ID or reference.

    Accepts UUIDs directly, plus project-scoped ``#N`` and ``N`` seq_num
    references when ``project_id`` is provided.
    """
    is_seq_ref = task_id.startswith("#") or task_id.isdigit()

    if is_seq_ref:
        if not project_id:
            raise ValueError(f"Task {task_id} requires project_id for seq_num lookup")
        try:
            resolved_id = resolve_task_reference(db, task_id, project_id)
            task_id = resolved_id
        except TaskNotFoundError as e:
            raise ValueError(str(e)) from e
    elif not _is_task_uuid(task_id):
        raise ValueError(f"Task {task_id} not found")

    if project_id:
        row = db.fetchone(
            "SELECT * FROM tasks WHERE id = %s AND project_id = %s",
            (task_id, project_id),
        )
    else:
        row = db.fetchone("SELECT * FROM tasks WHERE id = %s", (task_id,))
    if not row:
        if project_id:
            raise ValueError(f"Task {task_id} not found in project")
        raise ValueError(f"Task {task_id} not found")
    task = Task.from_row(row)
    hydrate_task_stage_state(db, [task])
    hydrate_task_blocking_state(db, [task])
    return task


def find_task_by_prefix(db: HubDatabase, prefix: str) -> Task | None:
    """Find a task by ID prefix. Returns None if no match or multiple matches."""
    prefix = prefix.strip()
    if not prefix:
        return None

    if _is_task_uuid(prefix):
        row = db.fetchone("SELECT * FROM tasks WHERE id = %s", (prefix,))
        if row:
            task = Task.from_row(row)
            hydrate_task_stage_state(db, [task])
            hydrate_task_blocking_state(db, [task])
            return task

    rows = db.fetchall(
        "SELECT * FROM tasks WHERE id::text LIKE %s ESCAPE '\\'",
        (f"{_escape_like_prefix(prefix)}%",),
    )
    if len(rows) == 1:
        task = Task.from_row(rows[0])
        hydrate_task_stage_state(db, [task])
        hydrate_task_blocking_state(db, [task])
        return task
    return None


def find_tasks_by_prefix(db: HubDatabase, prefix: str) -> list[Task]:
    """Find all tasks matching an ID prefix."""
    prefix = prefix.strip()
    if not prefix:
        return []

    rows = db.fetchall(
        "SELECT * FROM tasks WHERE id::text LIKE %s ESCAPE '\\'",
        (f"{_escape_like_prefix(prefix)}%",),
    )
    tasks = [Task.from_row(row) for row in rows]
    hydrate_task_stage_state(db, tasks)
    hydrate_task_blocking_state(db, tasks)
    return tasks
