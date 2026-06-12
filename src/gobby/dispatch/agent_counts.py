"""Agent run counting helpers shared by dispatch surfaces."""

from __future__ import annotations

from gobby.storage.hub.protocol import HubDatabase


def count_active_agents(db: HubDatabase | None, project_id: str | None = None) -> int:
    """Return pending/running agent runs, optionally scoped by parent-session project."""
    if db is None:
        return 0
    if project_id:
        row = db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM agent_runs ar
            JOIN sessions parent_s ON parent_s.id = ar.parent_session_id
            WHERE ar.status IN ('pending', 'running')
              AND parent_s.project_id = %s
            """,
            (project_id,),
        )
    else:
        row = db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM agent_runs
            WHERE status IN ('pending', 'running')
            """
        )
    return int(row["count"]) if row else 0
