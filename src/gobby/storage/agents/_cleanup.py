"""Cleanup operations for agent run storage."""

from __future__ import annotations

from typing import Protocol

from gobby.storage.daemon_resume_keys import RECONCILIATION_PENDING_KEY, RESUME_PHASE_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import (
    elapsed_seconds_greater_than_expr,
    json_text_expr,
    older_than_now_expr,
)
from gobby.utils.datetime import utc_now

from ._constants import logger
from ._lifecycle import terminal_fence_key
from ._models import AgentRun

# Provisional daemon-resume successors are exempt from stale sweeps only this
# long; past the bound a stuck non-finalized successor re-enters normal cleanup.
_PROVISIONAL_EXEMPTION_MINUTES = 60


class _AgentRunCleanupHost(Protocol):
    db: HubDatabase

    def timeout(
        self,
        run_id: str,
        turns_used: int = 0,
        error: str = "Execution timed out",
        tool_calls_count: int = 0,
    ) -> AgentRun | None: ...


class _AgentRunCleanupMixin:
    def cleanup_stale_runs(
        self: _AgentRunCleanupHost,
        *,
        machine_id: str,
        default_timeout_minutes: int = 30,
    ) -> list[str]:
        """Mark stale running agent runs as timed out and expire their sessions.

        Uses per-agent timeout_seconds when set, falls back to default_timeout_minutes.

        Args:
            default_timeout_minutes: Fallback timeout for runs without timeout_seconds.

        Returns:
            IDs of runs successfully transitioned to timeout.
        """
        explicit_timeout_sql = elapsed_seconds_greater_than_expr(
            self.db,
            "last_activity_at",
            "timeout_seconds",
        )
        default_timeout_sql = older_than_now_expr(self.db, "last_activity_at", "%s", "minute")
        pending_flag_sql = json_text_expr(
            self.db, "ar.resume_metadata_json", RECONCILIATION_PENDING_KEY
        )
        phase_sql = json_text_expr(self.db, "ar.resume_metadata_json", RESUME_PHASE_KEY)
        provisional_stale_sql = older_than_now_expr(self.db, "ar.updated_at", "%s", "minute")
        stale_runs = self.db.fetchall(
            f"""
            WITH run_activity AS (
                SELECT
                    ar.id,
                    ar.timeout_seconds,
                    ar.pid,
                    ar.terminal_id,
                    COALESCE(child.updated_at, ar.updated_at, ar.started_at) AS last_activity_at,
                    COALESCE(child.tool_call_count, parent.tool_call_count, ar.tool_calls_count, 0)
                        AS tool_calls_count,
                    COALESCE(child.turn_count, parent.turn_count, ar.turns_used, 0) AS turns_used
                FROM agent_runs ar
                LEFT JOIN sessions child ON child.id = ar.child_session_id
                LEFT JOIN sessions parent ON parent.id = ar.parent_session_id
                WHERE ar.status = 'running'
                  AND ar.machine_id = %s
                  AND (
                        (
                            COALESCE({pending_flag_sql}, 'false') != 'true'
                            AND COALESCE({phase_sql}, '')
                                NOT IN ('prepared', 'launch_requested', 'runtime_persisted')
                        )
                        OR {provisional_stale_sql}
                  )
            )
            SELECT
                id,
                timeout_seconds,
                tool_calls_count,
                turns_used
            FROM run_activity
            WHERE pid IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM terminals t
                  WHERE t.id = run_activity.terminal_id
                    AND t.state IN ('pending', 'live')
              )
              AND (
                (
                    timeout_seconds IS NOT NULL
                    AND {explicit_timeout_sql}
                )
                OR (
                    timeout_seconds IS NULL
                    AND {default_timeout_sql}
                )
              )
            """,  # nosec B608 # timeout expressions are selected by storage dialect.
            (machine_id, _PROVISIONAL_EXEMPTION_MINUTES, default_timeout_minutes),
        )

        explicit_count = 0
        default_count = 0
        timed_out_ids: list[str] = []
        for row in stale_runs:
            timeout_seconds = row["timeout_seconds"]
            error = (
                f"Exceeded timeout ({int(timeout_seconds)}s)"
                if timeout_seconds is not None
                else f"Exceeded default timeout ({default_timeout_minutes}m)"
            )
            run_id = str(row["id"])
            try:
                updated = self.timeout(
                    run_id,
                    turns_used=row["turns_used"] or 0,
                    error=error,
                    tool_calls_count=row["tool_calls_count"] or 0,
                )
            except Exception:
                logger.warning("Failed to timeout stale agent run %s", run_id, exc_info=True)
                continue
            if updated is None:
                continue
            timed_out_ids.append(run_id)
            if timeout_seconds is not None:
                explicit_count += 1
            else:
                default_count += 1

        if timed_out_ids:
            logger.info(
                "Timed out %s stale agent runs (%s explicit, %s default)",
                len(timed_out_ids),
                explicit_count,
                default_count,
            )

        return timed_out_ids

    def cleanup_stale_pending_runs(
        self: _AgentRunCleanupHost,
        *,
        machine_id: str,
        timeout_minutes: int = 60,
        long_timeout_minutes: int = 1440,
    ) -> list[str]:
        """Mark stale pending agent runs as failed and return transitioned IDs."""
        now = utc_now()
        pending_timeout_sql = older_than_now_expr(self.db, "created_at", "%s", "minute")
        pending_flag_sql = json_text_expr(
            self.db, "resume_metadata_json", RECONCILIATION_PENDING_KEY
        )
        phase_sql = json_text_expr(self.db, "resume_metadata_json", RESUME_PHASE_KEY)
        provisional_stale_sql = older_than_now_expr(self.db, "updated_at", "%s", "minute")
        with self.db.bounded_transaction() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock_shared(%s)",
                (terminal_fence_key(),),
            )
            cursor = conn.execute(
                f"""
                UPDATE agent_runs
                SET status = 'error',
                    error = CASE
                        WHEN NOT EXISTS (
                            SELECT 1 FROM terminals started
                            WHERE started.id = agent_runs.terminal_id
                              AND started.state = 'live'
                        ) THEN 'Pending run never started'
                        ELSE 'Pending tmux-initialized run never started'
                    END,
                    pid = NULL,
                    completed_at = %s,
                    updated_at = %s
                WHERE status = 'pending'
                AND machine_id = %s
                AND (
                    (
                        COALESCE({pending_flag_sql}, 'false') != 'true'
                        AND COALESCE({phase_sql}, '')
                            NOT IN ('prepared', 'launch_requested', 'runtime_persisted')
                    )
                    OR {provisional_stale_sql}
                )
                AND (
                    (
                        NOT EXISTS (
                            SELECT 1 FROM terminals started
                            WHERE started.id = agent_runs.terminal_id
                              AND started.state = 'live'
                        )
                        AND {pending_timeout_sql}
                    )
                    OR (
                        EXISTS (
                            SELECT 1 FROM terminals started
                            WHERE started.id = agent_runs.terminal_id
                              AND started.state = 'live'
                        )
                        AND {pending_timeout_sql}
                    )
                )
                RETURNING id
                """,  # nosec B608 # timeout expression is selected by storage dialect.
                (
                    now,
                    now,
                    machine_id,
                    _PROVISIONAL_EXEMPTION_MINUTES,
                    timeout_minutes,
                    long_timeout_minutes,
                ),
            )
            run_ids = [str(row["id"]) for row in cursor.fetchall()]
        if run_ids:
            logger.info(
                "Failed %s stale pending agent runs (>%sm; tmux >%sm)",
                len(run_ids),
                timeout_minutes,
                long_timeout_minutes,
            )
        return run_ids
