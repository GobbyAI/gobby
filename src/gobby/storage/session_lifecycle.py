"""Session lifecycle operations.

Standalone functions for expiring and pausing inactive sessions.
Extracted from SessionManager as part of the Strangler Fig
decomposition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gobby.sessions.status_events import (
    SessionStatusTransition,
    SessionStatusTransitionCallback,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.sessions._constants import SESSION_REVIVAL_HORIZON_HOURS, SYSTEM_SESSION_SOURCE
from gobby.storage.sql_dialect import older_than_now_expr, table_column_names
from gobby.utils.datetime import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionStateCleanupResult:
    session_variables: int
    pending_interactions: int


_EMPTY_SESSION_PRUNE_REFERENCE_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sessions", ("parent_session_id",)),
    ("tasks", ("created_in_session_id", "closed_in_session_id", "claimed_by_session_id")),
    ("memories", ("source_session_id",)),
    ("agent_runs", ("parent_session_id", "child_session_id", "claimed_session_id")),
    ("workflow_audit_log", ("session_id",)),
    ("pending_approvals", ("session_id",)),
)


def rebind_agent_run(
    db: HubDatabase,
    *,
    session_id: str,
    expected_run_id: str,
    new_run_id: str,
    workflow_name: str | None,
) -> bool:
    """Atomically move a durable session's run back-pointer to a successor.

    A ``None`` workflow_name preserves the session's existing workflow binding;
    only a concrete value overwrites it.
    """
    cursor = db.execute(
        """
        UPDATE sessions
        SET agent_run_id = %s,
            workflow_name = COALESCE(%s, workflow_name),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND agent_run_id = %s
          AND status NOT IN ('expired', 'deleted')
        """,
        (new_run_id, workflow_name, session_id, expected_run_id),
    )
    return bool(cursor.rowcount)


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


def session_has_retained_references(db: HubDatabase, session_id: str) -> bool:
    """Return whether any durable record still references a session."""
    guards = _build_empty_session_prune_reference_guards(db)
    if not guards:
        return False
    row = db.fetchone(
        f"""
        SELECT 1 AS retained
        FROM sessions
        WHERE id = %s
          AND NOT ({" AND ".join(guards)})
        """,  # Guard SQL is generated from fixed identifiers above. # nosec B608
        (session_id,),
    )
    return row is not None


def expire_stale_sessions(
    db: HubDatabase,
    timeout_hours: int = 24,
    *,
    status_notifier: SessionStatusTransitionCallback | None = None,
) -> int:
    """
    Mark sessions as expired if they've been inactive for too long.

    Args:
        db: Database connection.
        timeout_hours: Hours of inactivity before expiring.

    Returns:
        Number of sessions expired.
    """
    # Intentionally global: inactivity expiry must cover sessions left by a sleeping machine.
    inactive_stale_sql = older_than_now_expr(db, "last_activity", "%s", "hour")
    empty_terminal_created_stale_sql = older_than_now_expr(db, "created_at", "%s", "hour")
    empty_terminal_context_sql = "terminal_context IS NULL"
    tmux_target_sql = """
        session_type = 'terminal'
        AND (
            NULLIF(BTRIM(terminal_context->>'tmux_pane'), '') IS NOT NULL
            OR NULLIF(BTRIM(terminal_context->>'tmux_window_id'), '') IS NOT NULL
        )
    """
    with db.transaction() as conn:
        rows = conn.execute(
            f"""
            UPDATE sessions
            SET status = 'expired', updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('active', 'paused', 'handoff_ready')
            AND source != %s
            AND NOT ({tmux_target_sql})
            AND (
                {inactive_stale_sql}
                OR (
                    session_type = 'terminal'
                    AND {empty_terminal_context_sql}
                    AND {empty_terminal_created_stale_sql}
                    AND {inactive_stale_sql}
                )
            )
            RETURNING *
            """,  # nosec B608 # cutoff expressions are selected by storage dialect.
            (SYSTEM_SESSION_SOURCE, timeout_hours, timeout_hours, timeout_hours),
        ).fetchall()
        if status_notifier is not None:
            for row in rows:
                status_notifier(SessionStatusTransition.from_session(Session.from_row(row)))
    count = len(rows)
    if count > 0:
        logger.info("Expired %s stale sessions (>%sh inactive)", count, timeout_hours)
    return count


def expire_orphaned_handoff_sessions(
    db: HubDatabase,
    timeout_minutes: int = 30,
    *,
    status_notifier: SessionStatusTransitionCallback | None = None,
) -> int:
    """
    Expire handoff_ready sessions whose compact restart never arrived.

    Compaction is an in-place handoff: the handoff_ready row IS the live
    session, so this sweep only flips status. Typed instances are kept for
    revival; prune_stale_compact_workflow_instances reclaims them once the
    revival horizon has passed.

    Args:
        db: Database connection.
        timeout_minutes: Minutes before orphaned handoff_ready sessions expire.

    Returns:
        Number of sessions expired.
    """
    updated_stale_sql = older_than_now_expr(db, "updated_at", "%s", "minute")
    with db.transaction() as conn:
        rows = conn.execute(
            f"""
            UPDATE sessions
            SET status = 'expired', updated_at = CURRENT_TIMESTAMP
            WHERE status = 'handoff_ready'
              AND source != %s
              AND {updated_stale_sql}
            RETURNING *
            """,  # nosec B608 # cutoff expression is selected by storage dialect.
            (SYSTEM_SESSION_SOURCE, timeout_minutes),
        ).fetchall()
        if status_notifier is not None:
            for row in rows:
                status_notifier(SessionStatusTransition.from_session(Session.from_row(row)))
    count = len(rows)
    if count > 0:
        logger.info("Expired %s orphaned handoff_ready sessions (>%sm)", count, timeout_minutes)
    return count


def prune_stale_compact_workflow_instances(
    db: HubDatabase,
    retention_hours: int = SESSION_REVIVAL_HORIZON_HOURS,
) -> int:
    """
    Delete typed agent-step instances for compact handoffs that never resumed.

    Targets only sessions expired beyond the revival horizon that still carry
    an unconsumed compact marker (the handoff_source session variable, cleared
    on successful in-place reactivation). Expired daemon-resume and ordinary
    sessions are untouched.

    Args:
        db: Database connection.
        retention_hours: Hours a session must be expired before reclamation.

    Returns:
        Number of workflow instances deleted.
    """
    updated_stale_sql = older_than_now_expr(db, "s.updated_at", "%s", "hour")
    cursor = db.execute(
        f"""
        DELETE FROM agent_step_instances wi
        USING sessions s, session_variables sv
        WHERE wi.session_id = s.id
          AND sv.session_id = s.id
          AND s.status = 'expired'
          AND s.source != %s
          AND sv.variables ? 'handoff_source'
          AND {updated_stale_sql}
        """,  # nosec B608 # cutoff expression is selected by storage dialect.
        (SYSTEM_SESSION_SOURCE, retention_hours),
    )
    count = cursor.rowcount or 0
    if count > 0:
        logger.info(
            "Pruned %s agent-step instances from unresumed compact sessions (>%sh expired)",
            count,
            retention_hours,
        )
    return count


def cleanup_expired_session_state(
    db: HubDatabase,
    horizon_hours: int = SESSION_REVIVAL_HORIZON_HOURS,
) -> SessionStateCleanupResult:
    """Atomically clear payloads and terminalize interactions past revival."""
    stale_sql = older_than_now_expr(db, "s.updated_at", "%s", "hour")
    with db.transaction() as conn:
        pending_cursor = conn.execute(
            f"""
            UPDATE pending_interactions pi
            SET status = 'expired',
                decision = 'timeout',
                resolved_at = CURRENT_TIMESTAMP
            FROM sessions s
            WHERE pi.session_id = s.id
              AND pi.status = 'pending'
              AND s.status IN ('expired', 'deleted')
              AND s.source != %s
              AND {stale_sql}
            """,  # nosec B608 # cutoff expression is selected by storage dialect.
            (SYSTEM_SESSION_SOURCE, horizon_hours),
        )
        variables_cursor = conn.execute(
            f"""
            DELETE FROM session_variables sv
            USING sessions s
            WHERE sv.session_id = s.id
              AND s.status IN ('expired', 'deleted')
              AND s.source != %s
              AND {stale_sql}
            """,  # nosec B608 # cutoff expression is selected by storage dialect.
            (SYSTEM_SESSION_SOURCE, horizon_hours),
        )

    result = SessionStateCleanupResult(
        session_variables=variables_cursor.rowcount or 0,
        pending_interactions=pending_cursor.rowcount or 0,
    )
    if result.session_variables or result.pending_interactions:
        logger.info(
            "Cleaned expired session state past %sh revival horizon: "
            "%s variable rows, %s pending interactions",
            horizon_hours,
            result.session_variables,
            result.pending_interactions,
        )
    return result


def pause_inactive_active_sessions(
    db: HubDatabase,
    timeout_minutes: int = 30,
    *,
    status_notifier: SessionStatusTransitionCallback | None = None,
) -> int:
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
    # Intentionally global: inactivity pausing must cover sessions left by a sleeping machine.
    transitioned_at = utc_now()
    inactive_stale_sql = older_than_now_expr(db, "last_activity", "%s", "minute")
    with db.transaction() as conn:
        rows = conn.execute(
            f"""
            UPDATE sessions
            SET status = 'paused'
            WHERE status = 'active'
            AND source != %s
            AND {inactive_stale_sql}
            RETURNING *
            """,  # nosec B608 # cutoff expression is selected by storage dialect.
            (SYSTEM_SESSION_SOURCE, timeout_minutes),
        ).fetchall()
        if status_notifier is not None:
            for row in rows:
                status_notifier(
                    SessionStatusTransition.from_session(
                        Session.from_row(row),
                        transitioned_at=transitioned_at,
                    )
                )
    count = len(rows)
    if count > 0:
        logger.info("Paused %s inactive active sessions (>%sm)", count, timeout_minutes)
    return count


def expire_empty_sessions(
    db: HubDatabase,
    timeout_hours: int = 2,
    *,
    status_notifier: SessionStatusTransitionCallback | None = None,
) -> int:
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
    inactive_stale_sql = older_than_now_expr(db, "last_activity", "%s", "hour")
    with db.transaction() as conn:
        rows = conn.execute(
            f"""
            UPDATE sessions
            SET status = 'expired', updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('active', 'paused')
            AND source != %s
            AND COALESCE(message_count, 0) = 0
            AND {inactive_stale_sql}
            RETURNING *
            """,  # nosec B608 # cutoff expression is selected by storage dialect.
            (SYSTEM_SESSION_SOURCE, timeout_hours),
        ).fetchall()
        if status_notifier is not None:
            for row in rows:
                status_notifier(SessionStatusTransition.from_session(Session.from_row(row)))
    count = len(rows)
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
        AND source != %s
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
        (SYSTEM_SESSION_SOURCE, *params),
    )
    candidate_count = row["count"] if row else 0
    reference_guards = "\n        AND ".join(_build_empty_session_prune_reference_guards(db))
    cursor = db.execute(
        f"""
        DELETE FROM sessions
        WHERE {base_where}
        {f"AND {reference_guards}" if reference_guards else ""}
        """,
        (SYSTEM_SESSION_SOURCE, *params),
    )
    count = cursor.rowcount or 0
    skipped = max(candidate_count - count, 0)
    if count > 0:
        logger.info(
            "Pruned %s empty ghost sessions (expired, 0 messages, >%sh)", count, min_age_hours
        )
    if skipped > 0:
        logger.debug(
            "Skipped pruning %s empty ghost sessions with retained references",
            skipped,
            extra={"skipped": skipped},
        )
    return count
