"""Eligibility and row locking for automatic isolation cleanup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

from psycopg import sql

from gobby.storage.hub.protocol import HubDatabase, Row
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import require_machine_id

CLOSED_TASK_CLEANUP_PREDICATE = """
    AND (w.task_id IS NULL OR EXISTS (
        SELECT 1 FROM tasks t WHERE t.id = w.task_id
          AND t.closed_at IS NOT NULL AND t.claimed_by_session_id IS NULL
    ))
"""


@contextmanager
def lock_isolation_for_cleanup(
    db: HubDatabase,
    table: Literal["worktrees", "clones"],
    workspace_id: str,
    *,
    expired_only: bool = True,
) -> Iterator[Row | None]:
    """Recheck eligibility and hold task then workspace locks through deletion.

    Skip locked candidates instead of waiting for a claim or another cleanup.
    The task-link recheck handles reassignment between the initial read and lock.
    """
    machine_id = require_machine_id()
    select = (
        sql.SQL("SELECT * FROM {} WHERE id = %s AND machine_id = %s")
        .format(sql.Identifier(table))
        .as_string()
    )
    with db.bounded_transaction(statement_timeout_ms=5_000, lock_timeout_ms=1_000) as conn:
        candidate = conn.execute(select, (workspace_id, machine_id)).fetchone()
        if candidate is None:
            yield None
            return
        task_id = candidate["task_id"]
        if task_id is not None:
            task = conn.execute(
                "SELECT closed_at, claimed_by_session_id FROM tasks "
                "WHERE id = %s FOR UPDATE SKIP LOCKED",
                (task_id,),
            ).fetchone()
            if task is None or task["closed_at"] is None or task["claimed_by_session_id"]:
                yield None
                return
        query = select + " AND agent_session_id IS NULL AND task_id IS NOT DISTINCT FROM %s"
        params = [workspace_id, machine_id, task_id]
        if expired_only:
            query += " AND status = 'merged' AND cleanup_after < %s"
            params.append(utc_now())
        row = conn.execute(query + " FOR UPDATE SKIP LOCKED", params).fetchone()
        yield row
