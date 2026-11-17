"""Agent-run lineage that a /clear successor takes over from its predecessor."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase


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
