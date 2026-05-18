"""Runtime state operations for agent run storage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from gobby.storage.database import DatabaseProtocol

from ._helpers import _positive_rowcount, utc_now_iso
from ._models import AgentRun


class _AgentRunRuntimeHost(Protocol):
    db: DatabaseProtocol

    def get(self, run_id: str) -> AgentRun | None: ...

    def _fetch_runs_with_live_stats(
        self,
        where_clause: str = "",
        params: Sequence[object] = (),
        *,
        order_by: str = "",
        limit: int | None = None,
    ) -> list[AgentRun]: ...


class _AgentRunRuntimeMixin:
    def update_sdk_session_id(
        self: _AgentRunRuntimeHost,
        run_id: str,
        sdk_session_id: str,
    ) -> AgentRun | None:
        """Store the SDK session ID for cross-mode resume.

        Args:
            run_id: The agent run ID.
            sdk_session_id: The Claude CLI session ID captured from ResultMessage.

        Returns:
            Updated AgentRun.
        """
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE agent_runs
            SET sdk_session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (sdk_session_id, now, run_id),
        )
        return self.get(run_id)

    def get_sdk_session_id_for_session(
        self: _AgentRunRuntimeHost,
        session_id: str,
    ) -> str | None:
        """Find SDK session ID for a session that was an agent run.

        Looks up agent_runs where child_session_id matches, returning
        the most recent sdk_session_id.
        """
        row = self.db.fetchone(
            """
            SELECT sdk_session_id FROM agent_runs
            WHERE child_session_id = ? AND sdk_session_id IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (session_id,),
        )
        return row["sdk_session_id"] if row else None

    def update_runtime(
        self: _AgentRunRuntimeHost,
        run_id: str,
        *,
        pid: int | None = None,
        tmux_session_name: str | None = None,
        worktree_id: str | None = None,
        clone_id: str | None = None,
    ) -> None:
        """Persist runtime state for an agent run (pid, tmux session, mode, isolation).

        Only updates fields that are provided (non-None).
        """
        updates: list[str] = []
        params: list[Any] = []

        if pid is not None:
            updates.append("pid = ?")
            params.append(pid)
        if tmux_session_name is not None:
            updates.append("tmux_session_name = ?")
            params.append(tmux_session_name)
        if worktree_id is not None:
            updates.append("worktree_id = ?")
            params.append(worktree_id)
        if clone_id is not None:
            updates.append("clone_id = ?")
            params.append(clone_id)

        if not updates:
            return

        now = utc_now_iso()
        updates.append("updated_at = ?")
        params.append(now)
        params.append(run_id)

        self.db.execute(
            f"UPDATE agent_runs SET {', '.join(updates)} WHERE id = ?",
            tuple(params),
        )

    def clear_tmux_session_name(
        self: _AgentRunRuntimeHost,
        run_id: str,
        tmux_session_name: str,
    ) -> bool:
        """Clear a persisted tmux session name if it still matches."""
        now = utc_now_iso()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET tmux_session_name = NULL, updated_at = ?
            WHERE id = ? AND tmux_session_name = ?
            """,
            (now, run_id, tmux_session_name),
        )
        return bool(_positive_rowcount(cursor))

    def list_pending_with_pid(self: _AgentRunRuntimeHost, limit: int = 100) -> list[AgentRun]:
        """List pending agent runs that have a PID (spawned but not yet marked running)."""
        return self._fetch_runs_with_live_stats(
            "WHERE ar.status = 'pending' AND ar.pid IS NOT NULL",
            order_by="ORDER BY ar.created_at ASC",
            limit=limit,
        )

    def update_child_session(
        self: _AgentRunRuntimeHost,
        run_id: str,
        child_session_id: str,
    ) -> AgentRun | None:
        """Update the child session ID for an agent run."""
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE agent_runs
            SET child_session_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (child_session_id, now, run_id),
        )
        return self.get(run_id)
