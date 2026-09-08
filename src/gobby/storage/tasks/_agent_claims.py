"""Shared guard for agent-facing single-task claim capacity."""

from gobby.storage.hub.protocol import Transaction
from gobby.storage.tasks._models import AgentTaskClaimConflictError


def ensure_agent_claim_available(
    conn: Transaction,
    session_id: str,
    *,
    target_task_id: str | None = None,
) -> None:
    """Raise when a session owns a different open task.

    The caller must hold ``AgentTaskClaimMutation`` for the session so the
    check and subsequent claim or creation stay atomic.
    """
    query = """
        SELECT id, seq_num
        FROM tasks
        WHERE claimed_by_session_id = %s
          AND closed_at IS NULL
    """
    params: tuple[str, ...] = (session_id,)
    if target_task_id is not None:
        query += " AND id <> %s"
        params = (session_id, target_task_id)
    query += " ORDER BY created_at, id LIMIT 1"

    row = conn.execute(query, params).fetchone()
    if row is None:
        return

    claimed_task_id = str(row["id"])
    seq_num = row["seq_num"]
    claimed_task_ref = f"#{seq_num}" if seq_num else claimed_task_id
    raise AgentTaskClaimConflictError(claimed_task_id, claimed_task_ref)
