"""Read and delete operations for agent run storage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from gobby.storage.daemon_resume_keys import (
    REAP_REQUESTED_AT_KEY,
    REAPED_AT_KEY,
    daemon_resume_unconsumed_condition,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import json_text_expr, newer_than_now_expr, older_than_now_expr

from ._constants import TERMINAL_AGENT_RUN_STATUSES, AgentRunStatus
from ._models import AgentRun


class _AgentRunQueryHost(Protocol):
    db: HubDatabase

    def _fetch_run_with_live_stats(
        self,
        where_clause: str,
        params: Sequence[object],
    ) -> AgentRun | None: ...

    def _fetch_runs_with_live_stats(
        self,
        where_clause: str = "",
        params: Sequence[object] = (),
        *,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AgentRun]: ...

    def _list_active_runs(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        machine_id: str | None,
        task_ids: Sequence[str] | None = None,
    ) -> list[AgentRun]: ...


class _AgentRunQueryMixin:
    def get(self: _AgentRunQueryHost, run_id: str) -> AgentRun | None:
        """Get agent run by ID."""
        return self._fetch_run_with_live_stats("WHERE ar.id = %s", (run_id,))

    def find_by_id_prefix(
        self: _AgentRunQueryHost,
        prefix: str,
        *,
        limit: int = 2,
    ) -> list[AgentRun]:
        """Find agent runs by a validated hexadecimal ID prefix."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.id::text LIKE %s",
            (f"{prefix}%",),
            order_by="ORDER BY ar.created_at DESC",
            limit=limit,
        )

    def has_active_run_for_task(self: _AgentRunQueryHost, task_id: str) -> bool:
        """Check if there's already a pending/running agent run for a task."""
        row = self.db.fetchone(
            "SELECT id FROM agent_runs WHERE task_id = %s AND status IN ('pending', 'running')",
            (task_id,),
        )
        return row is not None

    def get_active_run_for_task(self: _AgentRunQueryHost, task_id: str) -> AgentRun | None:
        """Get the active (pending/running) agent run for a task, if any."""
        runs = self._fetch_runs_with_live_stats(
            "WHERE ar.task_id = %s AND ar.status IN ('pending', 'running')",
            (task_id,),
            order_by="ORDER BY ar.created_at DESC",
            limit=1,
        )
        return runs[0] if runs else None

    def list_daemon_stop_resume_candidates(
        self: _AgentRunQueryHost,
        task_id: str,
        *,
        machine_id: str,
        limit: int = 20,
        max_age_hours: float = 24,
    ) -> list[AgentRun]:
        """List recent cancelled daemon-stop runs for task resume recovery."""
        if max_age_hours <= 0:
            raise ValueError("max_age_hours must be positive")
        unconsumed_sql = daemon_resume_unconsumed_condition(self.db, "ar.resume_metadata_json")
        recent_sql = newer_than_now_expr(
            self.db,
            "COALESCE(ar.completed_at, ar.updated_at, ar.created_at)",
            "%s",
            "hour",
        )
        return self._fetch_runs_with_live_stats(
            f"""
            WHERE ar.task_id = %s
              AND ar.machine_id = %s
              AND ar.status = 'cancelled'
              AND ar.terminal_reason = 'daemon_stop'
              AND {unconsumed_sql}
              AND {recent_sql}
            """,
            (task_id, machine_id, max_age_hours),
            order_by="ORDER BY ar.completed_at DESC NULLS LAST, ar.updated_at DESC",
            limit=limit,
        )

    def list_parked_non_task_resume_candidates(
        self: _AgentRunQueryHost,
        *,
        machine_id: str,
        limit: int = 20,
        max_age_hours: float = 24,
    ) -> list[AgentRun]:
        """List unconsumed non-task parked originals still owning their session.

        Task-owned parked runs are relaunched by the dispatcher; these have no
        dispatch owner and are retried by the lifecycle monitor instead.
        """
        if max_age_hours <= 0:
            raise ValueError("max_age_hours must be positive")
        unconsumed_sql = daemon_resume_unconsumed_condition(self.db, "ar.resume_metadata_json")
        recent_sql = newer_than_now_expr(
            self.db,
            "COALESCE(ar.completed_at, ar.updated_at, ar.created_at)",
            "%s",
            "hour",
        )
        return self._fetch_runs_with_live_stats(
            f"""
            WHERE ar.task_id IS NULL
              AND ar.machine_id = %s
              AND ar.status = 'cancelled'
              AND ar.terminal_reason = 'daemon_stop'
              AND {unconsumed_sql}
              AND {recent_sql}
              AND EXISTS (
                    SELECT 1
                    FROM sessions s
                    WHERE s.id = ar.child_session_id
                      AND s.agent_run_id = ar.id
                      AND s.status NOT IN ('expired', 'deleted')
              )
            """,
            (machine_id, max_age_hours),
            order_by="ORDER BY ar.completed_at ASC NULLS FIRST, ar.updated_at ASC",
            limit=limit,
        )

    def list_provisional_daemon_resumes(
        self: _AgentRunQueryHost,
        *,
        machine_id: str,
        limit: int = 100,
    ) -> list[AgentRun]:
        """List non-finalized successor runs requiring boot resolution."""
        phase_sql = json_text_expr(
            self.db,
            "ar.resume_metadata_json",
            "daemon_stop_resume_phase",
        )
        return self._fetch_runs_with_live_stats(
            f"""
            WHERE {phase_sql} IN ('prepared', 'launch_requested', 'runtime_persisted')
              AND ar.machine_id = %s
            """,
            (machine_id,),
            order_by="ORDER BY ar.created_at ASC, ar.id ASC",
            limit=limit,
        )

    def list_reconciliation_pending(
        self: _AgentRunQueryHost,
        *,
        machine_id: str,
        limit: int = 100,
    ) -> list[AgentRun]:
        """List active runs deferred by an unresolved startup inbox barrier."""
        pending_sql = json_text_expr(
            self.db,
            "ar.resume_metadata_json",
            "reconciliation_pending",
        )
        return self._fetch_runs_with_live_stats(
            f"""
            WHERE ar.status IN ('pending', 'running')
              AND ar.machine_id = %s
              AND {pending_sql} = 'true'
            """,
            (machine_id,),
            order_by="ORDER BY ar.updated_at ASC, ar.id ASC",
            limit=limit,
        )

    def list_daemon_stop_orphans(
        self: _AgentRunQueryHost,
        *,
        machine_id: str,
        max_age_hours: float | None = 24,
        limit: int = 100,
        task_ids: Sequence[str] | None = None,
    ) -> list[AgentRun]:
        """List parked originals whose durable-session recovery window elapsed.

        Parked originals flagged with an operator reap request
        (``daemon_stop_orphan_reap_requested_at``) match regardless of age.
        ``max_age_hours=None`` lists parked originals without any age gate;
        ``task_ids`` narrows results to runs owned by the given tasks.
        """
        if max_age_hours is not None and max_age_hours <= 0:
            raise ValueError("max_age_hours must be positive")
        unconsumed_sql = daemon_resume_unconsumed_condition(self.db, "ar.resume_metadata_json")
        reaped_at_sql = json_text_expr(
            self.db,
            "ar.resume_metadata_json",
            REAPED_AT_KEY,
        )
        conditions = [
            "ar.machine_id = %s",
            "ar.status = 'cancelled'",
            "ar.terminal_reason = 'daemon_stop'",
            unconsumed_sql,
            f"({reaped_at_sql} IS NULL OR {reaped_at_sql} = '')",
            """EXISTS (
                    SELECT 1
                    FROM sessions s
                    WHERE s.id = ar.child_session_id
                      AND s.agent_run_id = ar.id
              )""",
        ]
        params: list[object] = [machine_id]
        if max_age_hours is not None:
            requested_sql = json_text_expr(
                self.db,
                "ar.resume_metadata_json",
                REAP_REQUESTED_AT_KEY,
            )
            old_sql = older_than_now_expr(
                self.db,
                "COALESCE(ar.completed_at, ar.updated_at, ar.created_at)",
                "%s",
                "hour",
            )
            conditions.append(
                f"({old_sql} OR ({requested_sql} IS NOT NULL AND {requested_sql} <> ''))"
            )
            params.append(max_age_hours)
        if task_ids is not None:
            conditions.append("ar.task_id = ANY(%s)")
            params.append(list(task_ids))
        where_clause = "WHERE " + "\n              AND ".join(conditions)
        return self._fetch_runs_with_live_stats(
            where_clause,
            tuple(params),
            order_by="ORDER BY ar.completed_at ASC NULLS FIRST, ar.updated_at ASC",
            limit=limit,
        )

    def list_by_session(
        self: _AgentRunQueryHost,
        parent_session_id: str,
        status: AgentRunStatus | None = None,
        limit: int = 100,
    ) -> list[AgentRun]:
        """
        List agent runs for a session.

        Args:
            parent_session_id: The parent session ID.
            status: Optional status filter.
            limit: Maximum number of results.

        Returns:
            List of AgentRun objects.
        """
        conditions = ["ar.parent_session_id = %s"]
        params: list[object] = [parent_session_id]
        if status:
            conditions.append("ar.status = %s")
            params.append(status)

        return self._fetch_runs_with_live_stats(
            f"WHERE {' AND '.join(conditions)}",
            params,
            order_by="ORDER BY ar.created_at DESC",
            limit=limit,
        )

    def list_by_status(
        self: _AgentRunQueryHost,
        status: str | None = None,
        limit: int = 100,
        project_id: str | None = None,
        offset: int = 0,
    ) -> list[AgentRun]:
        """
        List agent runs, optionally filtered by status and/or project.

        Args:
            status: Optional status filter.
            limit: Maximum number of results.
            project_id: Optional project ID filter (joins through sessions).
            offset: Number of matching rows to skip.

        Returns:
            List of AgentRun objects.
        """
        conditions: list[str] = []
        params: list[object] = []

        if status:
            conditions.append("ar.status = %s")
            params.append(status)

        if project_id:
            conditions.append("parent_s.project_id = %s")
            params.append(project_id)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        return self._fetch_runs_with_live_stats(
            where_clause,
            params,
            order_by="ORDER BY ar.created_at DESC",
            limit=limit,
            offset=offset,
        )

    def list_running(self: _AgentRunQueryHost, limit: int = 100) -> list[AgentRun]:
        """List all currently running agent runs."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.status = 'running'",
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
        )

    def list_terminal_with_tmux(self: _AgentRunQueryHost, limit: int = 100) -> list[AgentRun]:
        """List terminal agent runs whose terminal row is still pending or live."""
        status_placeholders = ", ".join("%s" for _ in TERMINAL_AGENT_RUN_STATUSES)
        return self._fetch_runs_with_live_stats(
            f"""
            WHERE ar.status IN ({status_placeholders})
            AND EXISTS (
                SELECT 1 FROM terminals t
                WHERE t.id = ar.terminal_id
                  AND t.state IN ('pending', 'live')
            )
            """,
            TERMINAL_AGENT_RUN_STATUSES,
            order_by="ORDER BY ar.completed_at ASC, ar.updated_at ASC",
            limit=limit,
        )

    def _list_active_runs(
        self: _AgentRunQueryHost,
        limit: int = 100,
        offset: int = 0,
        *,
        machine_id: str | None,
        task_ids: Sequence[str] | None = None,
    ) -> list[AgentRun]:
        """List active runs under the requested explicit scope."""
        params: list[object] = []
        where_clause = "WHERE ar.status IN ('running', 'pending')"
        if machine_id is not None:
            where_clause += " AND ar.machine_id = %s"
            params.append(machine_id)
        if task_ids is not None:
            if not task_ids:
                return []
            placeholders = ", ".join("%s" for _ in task_ids)
            where_clause += f" AND ar.task_id IN ({placeholders})"
            params.extend(task_ids)
        return self._fetch_runs_with_live_stats(
            where_clause,
            params,
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
            offset=offset,
        )

    def list_active_for_machine(
        self: _AgentRunQueryHost,
        machine_id: str,
        limit: int = 100,
        offset: int = 0,
        *,
        task_ids: Sequence[str] | None = None,
    ) -> list[AgentRun]:
        """List active runs owned by one machine."""
        return self._list_active_runs(
            limit,
            offset,
            machine_id=machine_id,
            task_ids=task_ids,
        )

    def list_active_global(
        self: _AgentRunQueryHost,
        limit: int = 100,
        offset: int = 0,
        *,
        task_ids: Sequence[str] | None = None,
    ) -> list[AgentRun]:
        """List active runs across all machines."""
        return self._list_active_runs(
            limit,
            offset,
            machine_id=None,
            task_ids=task_ids,
        )

    def get_by_session(self: _AgentRunQueryHost, session_id: str) -> AgentRun | None:
        """Get active agent run by child session ID."""
        runs = self._fetch_runs_with_live_stats(
            "WHERE ar.child_session_id = %s AND ar.status IN ('running', 'pending')",
            (session_id,),
            order_by="ORDER BY ar.created_at DESC",
            limit=1,
        )
        return runs[0] if runs else None

    def list_by_parent(
        self: _AgentRunQueryHost,
        parent_session_id: str,
        limit: int = 100,
        status: AgentRunStatus | None = None,
    ) -> list[AgentRun]:
        """List active agent runs spawned by a parent session."""
        conditions = ["ar.parent_session_id = %s"]
        params: list[object] = [parent_session_id]
        if status:
            conditions.append("ar.status = %s")
            params.append(status)
        else:
            conditions.append("ar.status IN ('running', 'pending')")

        return self._fetch_runs_with_live_stats(
            f"WHERE {' AND '.join(conditions)}",
            params,
            order_by="ORDER BY ar.started_at ASC",
            limit=limit,
        )

    def count_by_session(self: _AgentRunQueryHost, parent_session_id: str) -> dict[str, int]:
        """
        Count agent runs by status for a session.

        Args:
            parent_session_id: The parent session ID.

        Returns:
            Dict mapping status to count.
        """
        rows = self.db.fetchall(
            """
            SELECT status, COUNT(*) as count
            FROM agent_runs
            WHERE parent_session_id = %s
            GROUP BY status
            """,
            (parent_session_id,),
        )
        return {row["status"]: row["count"] for row in rows}

    def get_cancelled_session_ids(
        self: _AgentRunQueryHost,
        since_hours: int = 24,
        agent_name: str | None = None,
    ) -> set[str]:
        """Return child session IDs for agent runs cancelled within the recency window."""
        from gobby.storage.sql_dialect import newer_than_now_expr

        recency_sql = newer_than_now_expr(self.db, "completed_at", "%s", "hour")
        sql = (
            "SELECT child_session_id FROM agent_runs "
            f"WHERE status = 'cancelled' AND child_session_id IS NOT NULL AND {recency_sql}"
        )  # nosec B608 # recency_sql is selected by storage dialect.
        params: list[int | str] = [since_hours]
        if agent_name is not None:
            sql += " AND agent_name = %s"
            params.append(agent_name)

        rows = self.db.fetchall(sql, params)
        return {row["child_session_id"] for row in rows}
