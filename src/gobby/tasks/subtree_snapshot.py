"""Capture a locked, authenticated descendant projection for lifecycle policy."""

import hashlib
import json

from pydantic import JsonValue, TypeAdapter

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.tasks import Task, TaskStaleStateError
from gobby.tasks.attestations import list_attestations

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_DESCENDANTS = """WITH RECURSIVE subtree(id) AS (
    SELECT id FROM tasks WHERE parent_task_id = %s
    UNION
    SELECT child.id FROM tasks child JOIN subtree parent ON child.parent_task_id = parent.id
)
SELECT id FROM subtree
"""


def capture_subtree_facts(db: HubDatabase, conn: Transaction, task_id: str) -> dict[str, JsonValue]:
    """Caller holds the root row; retain descendant locks in its transaction.

    Task creation/reparenting locks the target parent; evidence appends and task
    mutations lock their task. Lock every descendant without waiting for a child
    that may already be cascading toward this root, then recheck membership.
    A concurrent mutation asks the caller to retry rather than risking deadlock.
    """
    ids = sorted(str(row["id"]) for row in conn.execute(_DESCENDANTS, (task_id,)).fetchall())
    rows = (
        conn.execute(
            "SELECT * FROM tasks WHERE id = ANY(%s) ORDER BY id FOR NO KEY UPDATE SKIP LOCKED",
            (ids,),
        ).fetchall()
        if ids
        else []
    )
    if sorted(str(row["id"]) for row in rows) != ids:
        raise TaskStaleStateError(task_id)
    if sorted(str(row["id"]) for row in conn.execute(_DESCENDANTS, (task_id,)).fetchall()) != ids:
        raise TaskStaleStateError(task_id)
    descendants: list[JsonValue] = []
    for row in rows:
        task = Task.from_row(row)
        records = list_attestations(db, task.id)
        superseded = {record.supersedes_id for record in records if record.supersedes_id}
        descendants.append(
            {
                "task": _JSON_OBJECT.dump_python(task.to_dict(), mode="json"),
                "attestations": [
                    record.model_dump(mode="json")
                    for record in records
                    if record.id not in superseded
                ],
                "all_attestations": [record.model_dump(mode="json") for record in records],
            }
        )
    revision = hashlib.sha256(
        json.dumps(descendants, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return {
        "descendants": descendants,
        "descendants_closed": all(row["closed_at"] is not None for row in rows),
        "subtree_revision": revision,
    }
