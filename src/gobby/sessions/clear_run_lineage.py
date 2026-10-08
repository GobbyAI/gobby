"""Agent-run lineage that a /clear successor takes over from its predecessor."""

from __future__ import annotations

from collections.abc import Sequence

from gobby.storage.hub.protocol import HubDatabase, Transaction


def current_run_session_id(
    db: HubDatabase, *, agent_run_id: str | None, session_id: str, project_id: str
) -> str:
    """Return the session a run-bound capability now speaks for.

    A /clear hands a live run to the successor, but the pane keeps sending the
    predecessor it was launched as. Follows the run's live binding (the run's
    child naming the run back) and never walks the clear chain, so a capability
    without a live run keeps ``session_id``.
    """
    if agent_run_id is None:
        return session_id
    row = db.fetchone(
        """
        SELECT run.child_session_id FROM agent_runs run
        JOIN sessions child ON child.id = run.child_session_id
        WHERE run.id = %s AND run.status IN ('pending', 'running')
          AND child.agent_run_id = run.id AND child.project_id = %s
        """,
        (agent_run_id, project_id),
    )
    return str(row["child_session_id"]) if row is not None else session_id


def move_clear_run_lineage(
    conn: Transaction,
    *,
    predecessor_id: str,
    successor_id: str,
    session_ids: Sequence[str],
) -> None:
    """Hand the runs of ``session_ids`` and the predecessor's spawn depth to the successor.

    ``session_ids`` holds the predecessor and, when a newer successor supersedes a
    bound one, that stale successor. Runs inside the caller's clear transaction.
    """
    ids = list(session_ids)
    conn.execute(
        "UPDATE agent_runs SET parent_session_id = %s WHERE parent_session_id = ANY(%s)",
        (successor_id, ids),
    )
    moved = conn.execute(
        """
        UPDATE agent_runs SET child_session_id = %s, updated_at = now()
        WHERE child_session_id = ANY(%s) AND status IN ('pending', 'running')
        RETURNING id
        """,
        (successor_id, ids),
    ).fetchall()
    for row in moved:
        run_id = row["id"]
        conn.execute(
            "UPDATE sessions SET agent_run_id = NULL WHERE id = ANY(%s) AND agent_run_id = %s",
            (ids, run_id),
        )
        conn.execute(
            "UPDATE sessions SET agent_run_id = %s WHERE id = %s AND agent_run_id IS NULL",
            (run_id, successor_id),
        )
        conn.execute(
            """
            UPDATE terminals SET session_id = %s, updated_at = now()
            WHERE agent_run_id = %s AND state IN ('pending', 'live')
            """,
            (successor_id, run_id),
        )
    conn.execute(
        """
        UPDATE sessions SET agent_depth = predecessor.agent_depth,
                            spawned_by_agent_id = predecessor.spawned_by_agent_id
        FROM sessions predecessor
        WHERE sessions.id = %s AND predecessor.id = %s
        """,
        (successor_id, predecessor_id),
    )
