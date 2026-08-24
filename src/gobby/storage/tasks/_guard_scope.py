"""Scoped task fetch for cumulative epic guards.

Guard collection needs the closed leaves under a task's nearest epic ancestor.
It used to page every task in the project to find them -- 14,878 rows here,
each with its stage and blocking state hydrated on top -- and then keep only
the subtree. That walk was the hottest stack in a 66-second event-loop stall,
and still cost roughly 105 seconds per close_task preview after #20841 moved it
onto a worker thread, where it parks a shared db-executor slot for the duration
(#20847).

The scope is the task's ancestor chain plus the nearest epic ancestor's
subtree: the ancestors so nearest-epic resolution can walk upward, the subtree
so descendant and leaf detection can run. Nothing the guard graph reads lives
outside it.
"""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks._models import Task

# Matches the bound the sibling epic-descendant walk uses, and is far past any
# real nesting depth; the path arrays below are what actually stop a cycle.
_MAX_DEPTH = 100

_SCOPE_SQL = f"""
WITH RECURSIVE ancestors(id, parent_task_id, task_type, depth, path) AS (
    SELECT root.id, root.parent_task_id, root.task_type, 0, ARRAY[root.id]
      FROM tasks root
     WHERE root.id = %s
    UNION ALL
    SELECT parent.id,
           parent.parent_task_id,
           parent.task_type,
           ancestors.depth + 1,
           ancestors.path || parent.id
      FROM tasks parent
      JOIN ancestors ON parent.id = ancestors.parent_task_id
     WHERE ancestors.depth < {_MAX_DEPTH}
       AND NOT parent.id = ANY(ancestors.path)
),
nearest_epic AS (
    SELECT ancestors.id
      FROM ancestors
     WHERE ancestors.depth > 0
       AND ancestors.task_type = 'epic'
     ORDER BY ancestors.depth ASC
     LIMIT 1
),
subtree(id, depth, path) AS (
    SELECT nearest_epic.id, 0, ARRAY[nearest_epic.id]
      FROM nearest_epic
    UNION ALL
    SELECT child.id, subtree.depth + 1, subtree.path || child.id
      FROM tasks child
      JOIN subtree ON child.parent_task_id = subtree.id
     WHERE subtree.depth < {_MAX_DEPTH}
       AND NOT child.id = ANY(subtree.path)
)
SELECT tasks.*
  FROM tasks
 WHERE tasks.id IN (SELECT id FROM ancestors)
    OR tasks.id IN (SELECT id FROM subtree)
"""


def list_epic_guard_scope(db: HubDatabase, task_id: str) -> list[Task]:
    """Return the task's ancestors plus its nearest epic ancestor's subtree.

    Returns the ancestor chain alone when the task has no epic ancestor, which
    is what guard collection reads to conclude there is no epic to guard.
    """
    rows = db.fetchall(_SCOPE_SQL, (task_id,))
    return [Task.from_row(row) for row in rows]


__all__ = ["list_epic_guard_scope"]
