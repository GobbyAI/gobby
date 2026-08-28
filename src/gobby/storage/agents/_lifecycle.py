"""Lifecycle operations for agent run storage."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Protocol

from gobby.agents.resume_metadata import dump_resume_metadata
from gobby.deployment import deployment_advisory_key
from gobby.sessions.status_events import SessionStatusTransition, SessionStatusTransitionCallback
from gobby.storage.daemon_resume_keys import daemon_resume_consumed_condition
from gobby.storage.hub._ambient import ambient_transaction
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.utils.datetime import utc_now
from gobby.utils.machine_id import get_machine_id

from ._constants import TERMINAL_AGENT_RUN_STATUSES, AgentRunTerminalReason, logger
from ._helpers import _positive_rowcount
from ._models import AgentRun


class TerminalTransitionNestedError(RuntimeError):
    """Raised when a terminal transition is attempted inside an ambient transaction."""


def terminal_fence_key() -> int:
    """Return the deployment-scoped fence shared by terminal writers and boot recovery."""
    return deployment_advisory_key("agent-terminal-transition")


def _execute_terminal_transition(
    host: _AgentRunLifecycleHost,
    *,
    run_id: str,
    sql: str,
    params: Sequence[object],
) -> AgentRun | None:
    if ambient_transaction(host.db) is not None:
        raise TerminalTransitionNestedError(
            f"Terminal transition for agent {run_id} cannot run inside a transaction"
        )

    with host.db.bounded_transaction() as txn:
        txn.execute(
            "SELECT pg_advisory_xact_lock_shared(%s)",
            (terminal_fence_key(),),
        )
        cursor = txn.execute(sql, params)
        if not _positive_rowcount(cursor):
            return None
        now = utc_now()
        txn.execute(
            """
            UPDATE terminals
            SET state = 'exited',
                updated_at = %s,
                automatic_write_quarantined_at = NULL,
                automatic_write_quarantine_action_key = NULL
            WHERE id = (SELECT terminal_id FROM agent_runs WHERE id = %s)
              AND state IN ('pending', 'live', 'orphaned')
            """,
            (now, run_id),
        )
        updated_run = host.get(run_id)
        if updated_run is None:
            return None
        host._transition_sessions_for_terminal_run(updated_run)

    credential_manager = host.credential_manager
    if credential_manager is not None:
        try:
            credential_manager.revoke(
                uuid.UUID(run_id),
                reason=f"agent-run-{updated_run.status}",
            )
        except Exception:
            logger.error("Managed credential revocation failed for terminal run %s", run_id)
    return updated_run


class _ManagedCredentialRevoker(Protocol):
    def revoke(
        self,
        managed_execution_id: uuid.UUID,
        *,
        generation: int | None = None,
        reason: str,
    ) -> object: ...


class _AgentRunLifecycleHost(Protocol):
    db: HubDatabase
    _status_notifier: SessionStatusTransitionCallback | None

    @property
    def credential_manager(self) -> _ManagedCredentialRevoker | None: ...

    def get(self, run_id: str) -> AgentRun | None: ...

    def _transition_sessions_for_terminal_run(self, run: AgentRun) -> int: ...


class _AgentRunLifecycleMixin:
    def create(
        self: _AgentRunLifecycleHost,
        parent_session_id: str,
        provider: str,
        prompt: str,
        workflow_name: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        is_local: bool = False,
        requested_reasoning_effort: str | None = None,
        effective_reasoning_effort: str | None = None,
        reasoning_required: bool = False,
        reasoning_status: str = "not_requested",
        reasoning_message: str | None = None,
        child_session_id: str | None = None,
        claimed_session_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        timeout_seconds: float | None = None,
        resume_metadata_json: Mapping[str, object] | None = None,
        worktree_id: str | None = None,
        clone_id: str | None = None,
    ) -> AgentRun:
        """
        Create a new agent run.

        Args:
            parent_session_id: Session that spawned this agent.
            provider: LLM provider (claude, qwen, etc.)
            prompt: The prompt given to the agent.
            workflow_name: Optional workflow being executed.
            agent_name: Agent definition name used to spawn the run.
            model: Optional model override.
            child_session_id: Optional child session for the agent.
            claimed_session_id: Session that owned the task when the run was created.
            run_id: Optional pre-generated run ID. If not provided, one is generated.
            task_id: Optional task ID this agent is working on.
            worktree_id: Registered isolation worktree the run executes in, if any.
                Persisted at creation so the prelaunch credential can bind its
                code-index overlay.
            clone_id: Registered isolation clone the run executes in, if any.

        Returns:
            Created AgentRun.
        """
        if run_id is None:
            run_id = str(uuid.uuid4())
        machine_id = get_machine_id()
        if machine_id is None:
            raise RuntimeError("Local machine identity is required to create an agent run")

        self.db.execute(
            """
            INSERT INTO agent_runs (
                id, machine_id, parent_session_id, child_session_id, claimed_session_id,
                workflow_name, agent_name,
                provider, model, is_local,
                requested_reasoning_effort, effective_reasoning_effort,
                reasoning_required, reasoning_status, reasoning_message,
                status, prompt, task_id, timeout_seconds, resume_metadata_json,
                worktree_id, clone_id
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'pending', %s, %s, %s, %s, %s, %s
            )
            """,
            (
                run_id,
                machine_id,
                parent_session_id,
                child_session_id,
                claimed_session_id,
                workflow_name,
                agent_name,
                provider,
                model,
                bool(is_local),
                requested_reasoning_effort,
                effective_reasoning_effort,
                bool(reasoning_required),
                reasoning_status,
                reasoning_message,
                prompt,
                task_id,
                timeout_seconds,
                dump_resume_metadata(resume_metadata_json),
                worktree_id,
                clone_id,
            ),
        )

        logger.debug(
            "Created agent run %s for session %s",
            run_id,
            parent_session_id,
        )
        agent_run = self.get(run_id)
        if agent_run is None:
            raise RuntimeError(f"Failed to retrieve newly created agent run: {run_id}")
        return agent_run

    def start(self: _AgentRunLifecycleHost, run_id: str) -> AgentRun | None:
        """Mark agent run as started."""
        now = utc_now()
        cursor = self.db.execute(
            """
            UPDATE agent_runs
            SET status = 'running', started_at = %s, updated_at = %s
            WHERE id = %s
              AND status = 'pending'
            """,
            (now, now, run_id),
        )
        if not _positive_rowcount(cursor):
            return None
        return self.get(run_id)

    def _transition_sessions_for_terminal_run(
        self: _AgentRunLifecycleHost,
        run: AgentRun,
    ) -> int:
        """Pause parked sessions and expire sessions for genuine terminal outcomes."""
        status = "paused" if run.terminal_reason == "daemon_stop" else "expired"
        now = utc_now()
        rows = self.db.execute(
            """
            SELECT *
            FROM sessions
            WHERE status IN ('active', 'paused')
              AND agent_run_id = %s
            FOR UPDATE
            """,
            (run.id,),
        ).fetchall()
        cursor = self.db.execute(
            """
            UPDATE sessions
            SET status = %s,
                updated_at = %s
            WHERE status IN ('active', 'paused')
              AND agent_run_id = %s
            """,
            (status, now, run.id),
        )
        if self._status_notifier is not None:
            for row in rows:
                session = Session.from_row(row)
                if session.status != status:
                    self._status_notifier(
                        SessionStatusTransition.from_session(
                            session,
                            status=status,
                            transitioned_at=now,
                        )
                    )
        return _positive_rowcount(cursor)

    def expire_sessions_for_terminal_runs(self: _AgentRunLifecycleHost) -> int:
        """Expire active/paused child sessions whose agent run is already terminal."""
        now = utc_now()
        consumed_sql = daemon_resume_consumed_condition(self.db, "ar.resume_metadata_json")
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"""
                UPDATE sessions
                SET status = 'expired',
                    updated_at = %s
                WHERE status IN ('active', 'paused')
                  AND EXISTS (
                        SELECT 1
                        FROM agent_runs ar
                        WHERE ar.id = sessions.agent_run_id
                          AND ar.status = ANY(%s)
                          AND (
                                ar.terminal_reason IS DISTINCT FROM 'daemon_stop'
                                OR {consumed_sql}
                          )
                  )
                RETURNING *
                """,
                (now, list(TERMINAL_AGENT_RUN_STATUSES)),
            ).fetchall()
            if self._status_notifier is not None:
                for row in rows:
                    self._status_notifier(
                        SessionStatusTransition.from_session(Session.from_row(row))
                    )
        return len(rows)

    def complete(
        self: _AgentRunLifecycleHost,
        run_id: str,
        result: str | None = None,
        tool_calls_count: int = 0,
        turns_used: int = 0,
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> AgentRun | None:
        """
        Mark agent run as completed successfully.

        Args:
            run_id: The agent run ID.
            result: Optional agent output/result override. When omitted, the
                current stored result is preserved.
            tool_calls_count: Number of tool calls made.
            turns_used: Number of turns used.
            terminal_reason: Optional reason for successful terminalization.

        Returns:
            Updated AgentRun.
        """
        now = utc_now()
        return _execute_terminal_transition(
            self,
            run_id=run_id,
            sql="""
            UPDATE agent_runs
            SET status = 'success',
                result = COALESCE(%s, result),
                terminal_reason = %s,
                pending_terminal_action = NULL,
                pending_terminal_reason = NULL,
                termination_requested_at = NULL,
                pid = NULL,
                tool_calls_count = %s,
                turns_used = %s,
                completed_at = %s,
                updated_at = %s
            WHERE id = %s
              AND status IN ('pending', 'running')
            """,
            params=(result, terminal_reason, tool_calls_count, turns_used, now, now, run_id),
        )

    def fail(
        self: _AgentRunLifecycleHost,
        run_id: str,
        error: str,
        tool_calls_count: int = 0,
        turns_used: int = 0,
        result: str | None = None,
    ) -> AgentRun | None:
        """
        Mark agent run as failed.

        Args:
            run_id: The agent run ID.
            error: Error message.
            tool_calls_count: Number of tool calls made before failure.
            turns_used: Number of turns used before failure.

        Returns:
            Updated AgentRun.
        """
        now = utc_now()
        return _execute_terminal_transition(
            self,
            run_id=run_id,
            sql="""
            UPDATE agent_runs
            SET status = 'error',
                error = %s,
                result = COALESCE(%s, result),
                terminal_reason = NULL,
                pending_terminal_action = NULL,
                pending_terminal_reason = NULL,
                termination_requested_at = NULL,
                pid = NULL,
                tool_calls_count = %s,
                turns_used = %s,
                completed_at = %s,
                updated_at = %s
            WHERE id = %s
              AND status IN ('pending', 'running')
            """,
            params=(error, result, tool_calls_count, turns_used, now, now, run_id),
        )

    def timeout(
        self: _AgentRunLifecycleHost,
        run_id: str,
        turns_used: int = 0,
        error: str = "Execution timed out",
        tool_calls_count: int = 0,
        result: str | None = None,
    ) -> AgentRun | None:
        """Mark agent run as timed out."""
        now = utc_now()
        return _execute_terminal_transition(
            self,
            run_id=run_id,
            sql="""
            UPDATE agent_runs
            SET status = 'timeout',
                error = %s,
                result = COALESCE(%s, result),
                terminal_reason = NULL,
                pending_terminal_action = NULL,
                pending_terminal_reason = NULL,
                termination_requested_at = NULL,
                pid = NULL,
                tool_calls_count = %s,
                turns_used = %s,
                completed_at = %s,
                updated_at = %s
            WHERE id = %s
              AND status IN ('pending', 'running')
            """,
            params=(error, result, tool_calls_count, turns_used, now, now, run_id),
        )

    def cancel(
        self: _AgentRunLifecycleHost,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason | None = None,
        result: str | None = None,
    ) -> AgentRun | None:
        """Mark agent run as cancelled."""
        now = utc_now()
        return _execute_terminal_transition(
            self,
            run_id=run_id,
            sql="""
            UPDATE agent_runs
            SET status = 'cancelled',
                terminal_reason = %s,
                result = COALESCE(%s, result),
                pending_terminal_action = NULL,
                pending_terminal_reason = NULL,
                termination_requested_at = NULL,
                pid = NULL,
                completed_at = %s,
                updated_at = %s
            WHERE id = %s
              AND status IN ('pending', 'running')
            """,
            params=(terminal_reason, result, now, now, run_id),
        )
