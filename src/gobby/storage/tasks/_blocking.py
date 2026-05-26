"""Helpers for hydrating task dependency blocking state."""

from __future__ import annotations

from collections.abc import Sequence

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import Task


def _fetch_all_blocker_map(db: HubDatabase, task_ids: Sequence[str]) -> dict[str, set[str]]:
    """Return all direct blocker IDs for the given tasks."""
    if not task_ids:
        return {}

    placeholders = ", ".join("?" for _ in task_ids)
    rows = db.fetchall(
        f"SELECT task_id, depends_on FROM task_dependencies "
        f"WHERE dep_type = 'blocks' AND task_id IN ({placeholders})",  # nosec B608
        tuple(task_ids),
    )

    blocker_map: dict[str, set[str]] = {}
    for row in rows:
        blocker_map.setdefault(row["task_id"], set()).add(row["depends_on"])
    return blocker_map


def _fetch_active_blocker_map(db: HubDatabase, task_ids: Sequence[str]) -> dict[str, set[str]]:
    """Return unresolved external blocker IDs for the given tasks."""
    if not task_ids:
        return {}

    placeholders = ", ".join("?" for _ in task_ids)
    rows = db.fetchall(
        f"""
        SELECT d.task_id, d.depends_on
        FROM task_dependencies d
        JOIN tasks blocked ON blocked.id = d.task_id
        JOIN tasks blocker ON blocker.id = d.depends_on
        WHERE d.dep_type = 'blocks'
          AND d.task_id IN ({placeholders})
          AND blocker.closed_at IS NULL
          AND NOT EXISTS (
              WITH RECURSIVE ancestors AS (
                  SELECT blocker.parent_task_id AS ancestor_id
                  UNION ALL
                  SELECT p.parent_task_id
                  FROM tasks p
                  JOIN ancestors a ON p.id = a.ancestor_id
                  WHERE p.parent_task_id IS NOT NULL
              )
              SELECT 1 FROM ancestors WHERE ancestor_id = blocked.id
          )
        """,  # nosec B608
        tuple(task_ids),
    )

    blocker_map: dict[str, set[str]] = {}
    for row in rows:
        blocker_map.setdefault(row["task_id"], set()).add(row["depends_on"])
    return blocker_map


def hydrate_task_blocking_state(db: HubDatabase, tasks: Sequence[Task]) -> None:
    """Populate dependency-ordering and canonical blocked-state fields on tasks."""
    if not tasks:
        return

    task_ids = [task.id for task in tasks]
    all_blocker_map = _fetch_all_blocker_map(db, task_ids)
    active_blocker_map = _fetch_active_blocker_map(db, task_ids)

    for task in tasks:
        task.blocked_by = all_blocker_map.get(task.id, set())
        task.active_blocked_by = active_blocker_map.get(task.id, set())
