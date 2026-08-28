"""Runtime state operations for agent run storage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from gobby.agents.resume_metadata import dump_resume_metadata
from gobby.storage.daemon_resume_keys import RESUME_PHASE_KEY
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

from ._helpers import _positive_rowcount
from ._models import AgentRun

_UNSET: Any = object()


class _AgentRunRuntimeHost(Protocol):
    db: HubDatabase

    def get(self, run_id: str) -> AgentRun | None: ...

    def _fetch_runs_with_live_stats(
        self,
        where_clause: str = "",
        params: Sequence[object] = (),
        *,
        order_by: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AgentRun]: ...


class _AgentRunRuntimeMixin:
    def update_resume_metadata(
        self: _AgentRunRuntimeHost,
        run_id: str,
        resume_metadata_json: Mapping[str, Any] | None,
    ) -> AgentRun | None:
        """Replace the daemon-stop resume launch snapshot for a run."""
        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET resume_metadata_json = %s, updated_at = %s
                WHERE id = %s
                """,
                (dump_resume_metadata(resume_metadata_json), now, run_id),
            )
        if not _positive_rowcount(cursor):
            return None
        return self.get(run_id)

    def merge_resume_metadata(
        self: _AgentRunRuntimeHost,
        run_id: str,
        updates: Mapping[str, Any],
    ) -> AgentRun | None:
        """Atomically merge top-level keys into resume metadata."""
        if not updates:
            return self.get(run_id)
        now = utc_now()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET resume_metadata_json =
                    COALESCE(resume_metadata_json, '{}'::jsonb) || %s::jsonb,
                updated_at = %s
            WHERE id = %s
            """,
            (dump_resume_metadata(updates), now, run_id),
        )
        if not _positive_rowcount(cursor):
            return None
        return self.get(run_id)

    def transition_resume_phase(
        self: _AgentRunRuntimeHost,
        run_id: str,
        *,
        expected_phase: str,
        new_phase: str,
        updates: Mapping[str, Any] | None = None,
    ) -> AgentRun | None:
        """Advance a provisional resume only from the expected durable phase."""
        patch = dict(updates or {})
        patch[RESUME_PHASE_KEY] = new_phase
        now = utc_now()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET resume_metadata_json =
                    COALESCE(resume_metadata_json, '{}'::jsonb) || %s::jsonb,
                updated_at = %s
            WHERE id = %s
              AND resume_metadata_json ->> %s = %s
            """,
            (dump_resume_metadata(patch), now, run_id, RESUME_PHASE_KEY, expected_phase),
        )
        if not _positive_rowcount(cursor):
            return None
        return self.get(run_id)

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
        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET sdk_session_id = %s, updated_at = %s
                WHERE id = %s
                """,
                (sdk_session_id, now, run_id),
            )
        if not _positive_rowcount(cursor):
            return None
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
            WHERE child_session_id = %s AND sdk_session_id IS NOT NULL
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
        pid: int | None = _UNSET,
        terminal_id: str | None = None,
        worktree_id: str | None = None,
        clone_id: str | None = None,
    ) -> None:
        """Persist runtime state for an agent run (pid, terminal row, isolation).

        Only updates fields that are provided. Pass ``pid=None`` to clear the PID.
        """
        updates: list[str] = []
        params: list[Any] = []

        if pid is not _UNSET:
            updates.append("pid = %s")
            params.append(pid)
        if terminal_id is not None:
            updates.append("terminal_id = %s")
            params.append(terminal_id)
        if worktree_id is not None:
            updates.append("worktree_id = %s")
            params.append(worktree_id)
        if clone_id is not None:
            updates.append("clone_id = %s")
            params.append(clone_id)

        if not updates:
            return

        now = utc_now()
        updates.append("updated_at = %s")
        params.append(now)
        params.append(run_id)

        self.db.execute(
            f"UPDATE agent_runs SET {', '.join(updates)} WHERE id = %s",
            tuple(params),
        )

    def clear_live_terminal(
        self: _AgentRunRuntimeHost,
        run_id: str,
        terminal_id: str,
    ) -> bool:
        """Exit the linked terminal when it still matches, and clear the pane PID."""
        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE terminals
                SET state = 'exited', updated_at = %s
                WHERE id = %s
                  AND state IN ('pending', 'live', 'orphaned')
                  AND id = (SELECT terminal_id FROM agent_runs WHERE id = %s)
                """,
                (now, terminal_id, run_id),
            )
            if not _positive_rowcount(cursor):
                return False
            conn.execute(
                """
                UPDATE agent_runs
                SET pid = NULL, updated_at = %s
                WHERE id = %s AND terminal_id = %s
                """,
                (now, run_id, terminal_id),
            )
        return True

    def update_child_session(
        self: _AgentRunRuntimeHost,
        run_id: str,
        child_session_id: str,
    ) -> AgentRun | None:
        """Update the child session ID for an agent run."""
        now = utc_now()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET child_session_id = %s, updated_at = %s
                WHERE id = %s
                """,
                (child_session_id, now, run_id),
            )
        if not _positive_rowcount(cursor):
            return None
        return self.get(run_id)
