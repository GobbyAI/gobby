"""Durable storage operations for serialized tmux termination."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

from ._models import AgentRun

TerminalAction = Literal["complete", "fail", "timeout", "cancel"]


class _AgentRunTerminationHost(Protocol):
    db: HubDatabase

    def get(self, run_id: str) -> AgentRun | None: ...

    def _fetch_runs_with_live_stats(
        self,
        where_clause: str = "",
        params: tuple[object, ...] = (),
        *,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AgentRun]: ...


class _AgentRunTerminationMixin:
    def record_termination_intent(
        self: _AgentRunTerminationHost,
        run_id: str,
        *,
        action: TerminalAction,
        reason: str | None = None,
        result_prefix: str | None = None,
        requested_at: datetime | None = None,
    ) -> AgentRun | None:
        """Persist retryable terminal intent before any destructive operation."""
        now = requested_at or utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET pending_terminal_action = %s,
                    pending_terminal_reason = %s,
                    termination_requested_at = COALESCE(termination_requested_at, %s),
                    result = CASE
                        WHEN COALESCE(result, '') = '' AND COALESCE(%s, '') <> '' THEN %s
                        ELSE result
                    END,
                    updated_at = %s
                WHERE id = %s
                  AND status IN ('pending', 'running')
                """,
                (action, reason, now, result_prefix, result_prefix, now, run_id),
            )
        if not cursor.rowcount:
            return None
        return self.get(run_id)

    def replace_capture_slot(
        self: _AgentRunTerminationHost,
        run_id: str,
        *,
        capture_id: str,
        expected_revision: int,
        marker: str,
        slot_content: str,
    ) -> AgentRun | None:
        """Initialize or CAS-replace the delimited capture suffix."""
        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET result = CASE
                        WHEN capture_id IS NULL THEN
                            CASE
                                WHEN COALESCE(result, '') = '' THEN %s
                                ELSE result || E'\n\n' || %s
                            END
                        ELSE LEFT(result, STRPOS(result, %s) - 1) || %s
                    END,
                    capture_id = %s,
                    capture_revision = capture_revision + 1,
                    updated_at = %s
                WHERE id = %s
                  AND status IN ('pending', 'running')
                  AND (
                      (capture_id IS NULL AND capture_revision = 0 AND %s = 0)
                      OR (capture_id = %s AND capture_revision = %s)
                  )
                  AND (capture_id IS NULL OR STRPOS(COALESCE(result, ''), %s) > 0)
                """,
                (
                    slot_content,
                    slot_content,
                    marker,
                    slot_content,
                    capture_id,
                    now,
                    run_id,
                    expected_revision,
                    capture_id,
                    expected_revision,
                    marker,
                ),
            )
        if not cursor.rowcount:
            return None
        return self.get(run_id)

    def list_termination_candidates(
        self: _AgentRunTerminationHost,
        *,
        machine_id: str,
        limit: int = 100,
    ) -> list[AgentRun]:
        """List durable intents and terminal-child fallback candidates."""
        return self._fetch_runs_with_live_stats(
            """
            WHERE ar.status IN ('pending', 'running')
              AND ar.machine_id = %s
              AND (
                  ar.pending_terminal_action IS NOT NULL
                  OR (
                      EXISTS (
                          SELECT 1 FROM terminals live_terminal
                          WHERE live_terminal.id = ar.terminal_id
                            AND live_terminal.state IN ('pending', 'live')
                      )
                      AND EXISTS (
                          SELECT 1
                          FROM sessions terminal_session
                          WHERE terminal_session.status IN ('expired', 'deleted')
                            AND (
                                terminal_session.id = ar.child_session_id
                                OR terminal_session.agent_run_id = ar.id
                            )
                      )
                  )
              )
            """,
            (machine_id,),
            order_by="ORDER BY ar.termination_requested_at NULLS LAST, ar.updated_at ASC",
            limit=limit,
        )
