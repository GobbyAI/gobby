"""Agent-run lineage that a /clear successor takes over from its predecessor."""

from __future__ import annotations

from collections.abc import Sequence

from gobby.storage.hub.protocol import HubDatabase, Transaction


def current_run_session_id(
    db: HubDatabase, *, agent_run_id: str | None, session_id: str, project_id: str
) -> str:
    """Return the session a run-bound capability now speaks for.

    Follow only a live mutual binding between the run and its child in this
    project. Never infer authority from clear lineage. A capability without a
    live run keeps ``session_id``; spawned clear cannot transfer a run.
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


def earlier_clear_session_id(
    db: HubDatabase,
    *,
    current_session_id: str,
    origin_session_id: str,
    project_id: str,
    external_ids: Sequence[str],
) -> str | None:
    """Return the session owning ``external_ids`` when it is an earlier clear of the pane.

    Every take parents its successor on the predecessor, including a successor
    that supersedes an unpulled one, so each session the pane's run has passed
    through reaches ``origin_session_id``, the session the pane's token names,
    by its parents.
    """
    row = db.fetchone(
        """
        WITH RECURSIVE chain (id, parent_session_id, owner_id) AS (
            SELECT id, parent_session_id, id FROM sessions
            WHERE project_id = %s AND external_id = ANY(%s) AND id <> %s
            UNION
            SELECT parent.id, parent.parent_session_id, chain.owner_id
            FROM sessions parent JOIN chain ON parent.id = chain.parent_session_id
            WHERE chain.id <> %s
        )
        SELECT owner_id FROM chain WHERE id = %s LIMIT 1
        """,
        (
            project_id,
            list(external_ids),
            current_session_id,
            origin_session_id,
            origin_session_id,
        ),
    )
    return str(row["owner_id"]) if row is not None else None


def move_clear_run_lineage(
    conn: Transaction,
    *,
    predecessor_id: str,
    successor_id: str,
    session_ids: Sequence[str],
) -> None:
    """Move an operator's child runs to its clear successor.

    ``session_ids`` holds the predecessor and, when a newer successor supersedes a
    bound one, that stale successor. Runs inside the caller's clear transaction.
    """
    ids = list(session_ids)
    spawned = conn.execute(
        """
        SELECT id FROM sessions WHERE id = ANY(%s)
          AND (agent_run_id IS NOT NULL OR agent_depth > 0 OR spawned_by_agent_id IS NOT NULL)
        FOR UPDATE
        """,
        (ids,),
    ).fetchone()
    if spawned is not None:
        raise ValueError("A spawned seat ends on clear and cannot have a clear successor")
    conn.execute(
        "UPDATE agent_runs SET parent_session_id = %s WHERE parent_session_id = ANY(%s)",
        (successor_id, ids),
    )
