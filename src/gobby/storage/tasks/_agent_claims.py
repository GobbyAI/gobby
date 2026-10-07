"""Shared guard for agent-facing single-task claim capacity."""

from collections.abc import Collection

from gobby.storage.hub.protocol import Transaction
from gobby.storage.tasks._models import AgentTaskClaimConflictError


def ensure_agent_claim_available(
    conn: Transaction,
    session_id: str,
    *,
    target_task_id: str | None = None,
    handed_off_task_ids: Collection[str] = (),
) -> None:
    """Raise when a session owns a different open task that is still active.

    ``handed_off_task_ids`` names the session's claims that wait only on review,
    landing or close (#23665); they keep their owner but no longer hold capacity.
    The caller must hold ``AgentTaskClaimMutation`` for the session so the
    check and subsequent claim or creation stay atomic.
    """
    query = """
        SELECT id, seq_num, escalated_at
        FROM tasks
        WHERE claimed_by_session_id = %s
          AND closed_at IS NULL
    """
    params: list[object] = [session_id]
    if target_task_id is not None:
        query += " AND id <> %s"
        params.append(target_task_id)
    if handed_off_task_ids:
        query += " AND NOT (id = ANY(%s::uuid[]))"
        params.append(sorted(handed_off_task_ids))
    query += " ORDER BY created_at, id LIMIT 1"

    row = conn.execute(query, tuple(params)).fetchone()
    if row is None:
        return

    claimed_task_id = str(row["id"])
    seq_num = row["seq_num"]
    claimed_task_ref = f"#{seq_num}" if seq_num else claimed_task_id
    raise AgentTaskClaimConflictError(
        claimed_task_id, claimed_task_ref, escalated=row["escalated_at"] is not None
    )
