"""Session lifecycle operations.

Standalone functions for expiring and pausing inactive sessions.
Extracted from SessionManager as part of the Strangler Fig
decomposition.
"""

from __future__ import annotations

import logging

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions._constants import SYSTEM_SESSION_ID
from gobby.storage.sql_dialect import older_than_now_expr, table_column_names

logger = logging.getLogger(__name__)

_EMPTY_SESSION_PRUNE_REFERENCE_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sessions", ("parent_session_id",)),
    ("tasks", ("created_in_session_id", "closed_in_session_id")),
    ("memories", ("source_session_id",)),
    ("agent_runs", ("parent_session_id", "child_session_id", "claimed_session_id")),
    ("workflow_audit_log", ("session_id",)),
    ("pending_approvals", ("session_id",)),
)


def transfer_compact_handoff_state(
    db: HubDatabase,
    parent_session_id: str,
    child_session_id: str,
) -> int:
    """Atomically move compact-resume workflow and agent-run ownership to a child."""
    if parent_session_id == child_session_id:
        raise ValueError("Compact handoff parent and child must be distinct sessions")

    with db.transaction() as conn:
        session_rows = conn.execute(
            """
            SELECT id, parent_session_id, status, agent_run_id
            FROM sessions
            WHERE id IN (%s, %s)
            ORDER BY id
            FOR UPDATE
            """,
            (parent_session_id, child_session_id),
        ).fetchall()
        sessions_by_id = {str(row["id"]): row for row in session_rows}
        parent = sessions_by_id.get(parent_session_id)
        child = sessions_by_id.get(child_session_id)
        if parent is None or child is None:
            raise ValueError("Compact handoff requires existing parent and child sessions")
        if parent["status"] != "handoff_ready":
            raise ValueError("Compact handoff parent must be handoff_ready")
        if str(child["parent_session_id"]) != parent_session_id:
            raise ValueError("Compact handoff child does not reference the parent session")
        if child["agent_run_id"] is not None:
            raise ValueError("Compact handoff child already owns an agent run")

        workflow_rows = conn.execute(
            """
            SELECT id, session_id
            FROM workflow_instances
            WHERE session_id IN (%s, %s)
            ORDER BY session_id, id
            FOR UPDATE
            """,
            (parent_session_id, child_session_id),
        ).fetchall()
        if any(str(row["session_id"]) == child_session_id for row in workflow_rows):
            raise ValueError("Compact handoff child already owns workflow state")

        agent_run_id = parent["agent_run_id"]
        if agent_run_id is not None:
            run_row = conn.execute(
                """
                SELECT id, child_session_id
                FROM agent_runs
                WHERE id = %s
                FOR UPDATE
                """,
                (agent_run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError("Compact handoff parent references a missing agent run")
            run_child_session_id = run_row["child_session_id"]
            if run_child_session_id is not None and str(run_child_session_id) != parent_session_id:
                raise ValueError("Compact handoff agent run belongs to another child session")

        moved_cursor = conn.execute(
            """
            UPDATE workflow_instances
            SET session_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE session_id = %s
            """,
            (child_session_id, parent_session_id),
        )
        conn.execute(
            """
            UPDATE sessions
            SET agent_run_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (agent_run_id, child_session_id),
        )
        if agent_run_id is not None:
            conn.execute(
                """
                UPDATE agent_runs
                SET child_session_id = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (child_session_id, agent_run_id),
            )
        conn.execute(
            """
            UPDATE sessions
            SET status = 'expired',
                agent_run_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (parent_session_id,),
        )

    return moved_cursor.rowcount or 0


def _build_empty_session_prune_reference_guards(db: HubDatabase) -> tuple[str, ...]:
    """Return guard clauses for retained session references present in this schema."""
    guards: list[str] = []

    for table_name, columns in _EMPTY_SESSION_PRUNE_REFERENCE_COLUMNS:
        existing_columns = table_column_names(db, table_name)
        if not existing_columns:
            continue

        matched_columns = [column for column in columns if column in existing_columns]
        if not matched_columns:
            continue

        alias = "ref"
        column_predicate = " OR ".join(
            f"{alias}.{column} = sessions.id" for column in matched_columns
        )
        guards.append(f"NOT EXISTS (SELECT 1 FROM {table_name} {alias} WHERE {column_predicate})")

    return tuple(guards)


def expire_stale_sessions(db: HubDatabase, timeout_hours: int = 24) -> int:
    """
    Mark sessions as expired if they've been inactive for too long.

    Args:
        db: Database connection.
        timeout_hours: Hours of inactivity before expiring.

    Returns:
        Number of sessions expired.
    """
    updated_stale_sql = older_than_now_expr(db, "updated_at", "%s", "hour")
    empty_terminal_created_stale_sql = older_than_now_expr(db, "created_at", "%s", "hour")
    empty_terminal_context_sql = "terminal_context IS NULL"
    cursor = db.execute(
        f"""
        UPDATE sessions
        SET status = 'expired', updated_at = CURRENT_TIMESTAMP
        WHERE status IN ('active', 'paused', 'handoff_ready')
        AND id != %s
        AND (
            {updated_stale_sql}
            OR (
                session_type = 'terminal'
                AND {empty_terminal_context_sql}
                AND {empty_terminal_created_stale_sql}
                AND {updated_stale_sql}
            )
        )
        """,  # nosec B608 # cutoff expressions are selected by storage dialect.
        (SYSTEM_SESSION_ID, timeout_hours, timeout_hours, timeout_hours),
    )
    count = cursor.rowcount or 0
    if count > 0:
        logger.info("Expired %s stale sessions (>%sh inactive)", count, timeout_hours)
    return count


def expire_orphaned_handoff_sessions(db: HubDatabase, timeout_minutes: int = 30) -> int:
    """
    Expire handoff_ready sessions that were never picked up by a child session.

    Legitimate handoffs complete within seconds. Any handoff_ready session
    older than timeout_minutes is orphaned and should be expired directly,
    rather than waiting for the 24-hour stale session sweep.

    Args:
        db: Database connection.
        timeout_minutes: Minutes before orphaned handoff_ready sessions expire.

    Returns:
        Number of sessions expired.
    """
    updated_stale_sql = older_than_now_expr(db, "updated_at", "%s", "minute")
    with db.transaction() as conn:
        cursor = conn.execute(
            f"""
            WITH orphaned AS (
                SELECT id
                FROM sessions
                WHERE status = 'handoff_ready'
                  AND id != %s
                  AND {updated_stale_sql}
                FOR UPDATE
            ),
            deleted_instances AS (
                DELETE FROM workflow_instances
                WHERE session_id IN (SELECT id FROM orphaned)
                RETURNING id
            )
            UPDATE sessions
            SET status = 'expired', updated_at = CURRENT_TIMESTAMP
            WHERE id IN (SELECT id FROM orphaned)
            """,  # nosec B608 # cutoff expression is selected by storage dialect.
            (SYSTEM_SESSION_ID, timeout_minutes),
        )
    count = cursor.rowcount or 0
    if count > 0:
        logger.info("Expired %s orphaned handoff_ready sessions (>%sm)", count, timeout_minutes)
    return count


def pause_inactive_active_sessions(db: HubDatabase, timeout_minutes: int = 30) -> int:
    """
    Mark active sessions as paused if they've been inactive for too long.

    This catches orphaned sessions that never received an AFTER_AGENT hook
    (e.g., Claude Code crashed mid-response).

    Args:
        db: Database connection.
        timeout_minutes: Minutes of inactivity before pausing.

    Returns:
        Number of sessions paused.
    """
    updated_stale_sql = older_than_now_expr(db, "updated_at", "%s", "minute")
    cursor = db.execute(
        f"""
        UPDATE sessions
        SET status = 'paused'
        WHERE status = 'active'
        AND id != %s
        AND {updated_stale_sql}
        """,  # nosec B608 # cutoff expression is selected by storage dialect.
        (SYSTEM_SESSION_ID, timeout_minutes),
    )
    count = cursor.rowcount or 0
    if count > 0:
        logger.info("Paused %s inactive active sessions (>%sm)", count, timeout_minutes)
    return count


def expire_empty_sessions(db: HubDatabase, timeout_hours: int = 2) -> int:
    """
    Fast-expire sessions that never received any messages.

    Normal stale expiration is intentionally conservative. Zero-message
    sessions created by spurious SESSION_START events can be expired much
    sooner once they have been idle long enough to rule out real activity.

    Args:
        db: Database connection.
        timeout_hours: Hours of inactivity before expiring empty sessions.

    Returns:
        Number of sessions expired.
    """
    updated_stale_sql = older_than_now_expr(db, "updated_at", "%s", "hour")
    cursor = db.execute(
        f"""
        UPDATE sessions
        SET status = 'expired', updated_at = CURRENT_TIMESTAMP
        WHERE status IN ('active', 'paused')
        AND id != %s
        AND COALESCE(message_count, 0) = 0
        AND {updated_stale_sql}
        """,  # nosec B608 # cutoff expression is selected by storage dialect.
        (SYSTEM_SESSION_ID, timeout_hours),
    )
    count = cursor.rowcount or 0
    if count > 0:
        logger.info(
            "Fast-expired %s empty sessions (0 messages, >%sh inactive)", count, timeout_hours
        )
    return count


def prune_empty_sessions(db: HubDatabase, min_age_hours: int = 1) -> int:
    """
    Hard-delete expired sessions that never received any messages.

    Runs after empty sessions have been expired. The extra age buffer ensures we
    only delete sessions that have been expired long enough to avoid racing any
    in-flight writes.

    Args:
        db: Database connection.
        min_age_hours: Hours an expired empty session must age before deletion.

    Returns:
        Number of sessions deleted.
    """
    params = (min_age_hours,)
    updated_stale_sql = older_than_now_expr(db, "updated_at", "%s", "hour")
    base_where = f"""
        status = 'expired'
        AND id != %s
        AND COALESCE(message_count, 0) = 0
        AND transcript_path IS NULL
        AND {updated_stale_sql}
    """  # nosec B608 # cutoff expression is selected by storage dialect.
    row = db.fetchone(
        f"""
        SELECT COUNT(*) AS count
        FROM sessions
        WHERE {base_where}
        """,
        (SYSTEM_SESSION_ID, *params),
    )
    candidate_count = row["count"] if row else 0
    reference_guards = "\n        AND ".join(_build_empty_session_prune_reference_guards(db))
    cursor = db.execute(
        f"""
        DELETE FROM sessions
        WHERE {base_where}
        {f"AND {reference_guards}" if reference_guards else ""}
        """,
        (SYSTEM_SESSION_ID, *params),
    )
    count = cursor.rowcount or 0
    skipped = max(candidate_count - count, 0)
    if count > 0:
        logger.info(
            "Pruned %s empty ghost sessions (expired, 0 messages, >%sh)", count, min_age_hours
        )
    if skipped > 0:
        logger.debug("Skipped pruning %s empty ghost sessions with retained references", skipped)
    return count
