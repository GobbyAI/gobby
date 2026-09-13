"""Bounded cleanup of terminal Ask observations, independent of source-index retention."""

from __future__ import annotations

import os

from gobby.storage.hub.protocol import HubDatabase


def retention_days() -> int:
    days = int(os.environ.get("GOBBY_ASK_RETENTION_DAYS", "7"))
    if not 1 <= days <= 3650:
        raise ValueError("Ask retention must be 1..3650 days")
    return days


def cleanup_expired(db: HubDatabase, *, days: int | None = None, limit: int = 100) -> int:
    """Lock a bounded batch; cascading FKs remove run-owned records atomically."""
    resolved_days = retention_days() if days is None else days
    if not 1 <= resolved_days <= 3650 or not 1 <= limit <= 1000:
        raise ValueError("invalid Ask retention bounds")
    with db.transaction() as conn:
        rows = conn.execute(
            """
            WITH expired AS (
                SELECT e.id FROM pipeline_executions e
                WHERE e.pipeline_name = 'native-ask'
                  AND e.status IN ('completed', 'failed', 'cancelled')
                  AND e.completed_at < NOW() - make_interval(days => %s)
                  AND (e.status <> 'failed' OR
                    (e.inputs_json::jsonb #>> '{ask,binding,deadline_at}')::timestamptz <= NOW())
                  AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(COALESCE(
                        e.inputs_json::jsonb #> '{ask,runtime,orchestration,attempts}', '[]'::jsonb
                    )) attempt JOIN agent_runs a ON a.id::text = attempt->>'agent_run_id'
                    WHERE a.status IN ('pending', 'running')
                  )
                ORDER BY e.completed_at, e.id LIMIT %s FOR UPDATE SKIP LOCKED
            )
            DELETE FROM pipeline_executions WHERE id IN (SELECT id FROM expired) RETURNING id
            """,
            (resolved_days, limit),
        ).fetchall()
    return len(rows)
