"""
Session coordinator module for session lifecycle management.

This module is extracted from hook_manager.py using Strangler Fig pattern.
It provides centralized session registration tracking, message caching,
and lifecycle coordination.

Classes:
    SessionCoordinator: Coordinates session lifecycle operations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

from gobby.agents.capture import capture_then_kill_sync
from gobby.hooks.session_types import HookSessionManager
from gobby.storage.agents import TerminalAction

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.worktrees import LocalWorktreeManager


_AUTH_PROMPT_RE = re.compile(
    r"/login|Press 1 to trust|not authenticated|Invalid API key|API key required",
    re.IGNORECASE,
)
_NO_ACTIVITY_ERROR = "Agent completed with no activity (0 tool calls, 0 turns)"
_INCOMPLETE_STEP_WORKFLOW_ERROR = "Agent session ended before step workflow completed"


def _format_no_activity_error(result: Any) -> str:
    if not isinstance(result, str) or not result.strip():
        return _NO_ACTIVITY_ERROR

    tail = "\n".join(result.splitlines()[-20:])
    if _AUTH_PROMPT_RE.search(tail):
        return (
            f"{_NO_ACTIVITY_ERROR} - auth/trust prompt detected in pane output. "
            "Check daemon-visible API/provider credentials or Claude Code login state."
        )
    return f"{_NO_ACTIVITY_ERROR} - last pane output:\n{tail}"


def _format_incomplete_step_workflow_error(
    workflow_name: str,
    current_step: str | None,
    exit_condition: str | None,
    *,
    eval_error: Exception | None = None,
) -> str:
    parts = [
        _INCOMPLETE_STEP_WORKFLOW_ERROR,
        f"workflow={workflow_name}",
        f"current_step={current_step or 'unknown'}",
    ]
    if exit_condition:
        parts.append(f"exit_condition={exit_condition}")
    else:
        parts.append("exit_condition=<none>")
    if eval_error is not None:
        parts.append(f"exit_condition_error={eval_error}")
    return "; ".join(parts)


class SessionCoordinator:
    """
    Coordinates session lifecycle operations.

    Provides centralized tracking for:
    - Session registration with daemon
    - Agent message caching between hooks
    - Session lifecycle transitions (completion, cleanup)

    Thread-safe for concurrent operations.

    Extracted from HookManager to separate session coordination concerns.
    """

    def __init__(
        self,
        session_storage: HookSessionManager | None = None,
        message_processor: Any | None = None,
        agent_run_manager: LocalAgentRunManager | None = None,
        worktree_manager: LocalWorktreeManager | None = None,
        logger: logging.Logger | None = None,
        completion_registry: Any | None = None,
    ) -> None:
        """
        Initialize SessionCoordinator.

        Args:
            session_storage: SessionManager for session queries
            message_processor: SessionMessageProcessor for message registration
            agent_run_manager: LocalAgentRunManager for agent run completion
            worktree_manager: LocalWorktreeManager for worktree release
            logger: Optional logger instance
            completion_registry: CompletionEventRegistry for notifying on agent completion
        """
        self._session_manager = session_storage
        self._message_processor = message_processor
        self._agent_run_manager = agent_run_manager
        self._worktree_manager = worktree_manager
        self.logger = logger or logging.getLogger(__name__)
        self._completion_registry = completion_registry

        # Session registration tracking (to avoid noisy logs)
        # Tracks which sessions have been registered with daemon
        self._registered_sessions: set[str] = set()
        self._registered_sessions_lock = threading.Lock()

        # Agent message cache (session_id -> (message, timestamp))
        # Used to pass agent responses from stop hook to post-tool-use hook
        self._agent_message_cache: dict[str, tuple[str, float]] = {}
        self._cache_lock = threading.Lock()

        # Per-session locks prevent duplicate registration without serializing unrelated hooks.
        self._lookup_locks: WeakValueDictionary[tuple[str, str], threading.Lock] = (
            WeakValueDictionary()
        )
        self._lookup_locks_lock = threading.Lock()

        # Reference to the main event loop for cross-thread async scheduling
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def set_completion_registry(self, registry: Any) -> None:
        """Inject completion registry after construction (avoids circular init ordering)."""
        self._completion_registry = registry
        # Capture the event loop at wiring time (called from the async startup path)
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    # ==================== REGISTRATION TRACKING ====================

    def register_session(self, session_id: str) -> None:
        """
        Mark a session as registered with the daemon.

        Args:
            session_id: The session ID to register
        """
        with self._registered_sessions_lock:
            self._registered_sessions.add(session_id)

    def unregister_session(self, session_id: str) -> None:
        """
        Remove a session from registration tracking.

        Args:
            session_id: The session ID to unregister
        """
        with self._registered_sessions_lock:
            self._registered_sessions.discard(session_id)

    def is_registered(self, session_id: str) -> bool:
        """
        Check if a session is registered with the daemon.

        Args:
            session_id: The session ID to check

        Returns:
            True if registered, False otherwise
        """
        with self._registered_sessions_lock:
            return session_id in self._registered_sessions

    def clear_registrations(self) -> None:
        """Clear all session registrations."""
        with self._registered_sessions_lock:
            self._registered_sessions.clear()

    # ==================== MESSAGE CACHING ====================

    def cache_agent_message(self, session_id: str, message: str) -> None:
        """
        Cache an agent message for later retrieval.

        Args:
            session_id: The session ID
            message: The message to cache
        """
        with self._cache_lock:
            self._agent_message_cache[session_id] = (message, time.time())

    def get_cached_message(
        self, session_id: str, max_age_seconds: float | None = None
    ) -> str | None:
        """
        Get a cached agent message.

        Args:
            session_id: The session ID
            max_age_seconds: Optional maximum age in seconds. If set, returns None
                           for messages older than this.

        Returns:
            The cached message, or None if not found or expired
        """
        with self._cache_lock:
            if session_id not in self._agent_message_cache:
                return None

            message, timestamp = self._agent_message_cache[session_id]

            if max_age_seconds is not None:
                age = time.time() - timestamp
                if age > max_age_seconds:
                    return None

            return message

    def clear_cached_message(self, session_id: str) -> None:
        """
        Clear a cached agent message.

        Args:
            session_id: The session ID
        """
        with self._cache_lock:
            self._agent_message_cache.pop(session_id, None)

    # ==================== LOOKUP LOCK ====================

    def get_lookup_lock(self, external_id: str, source: str) -> threading.Lock:
        """Get the lock that serializes lookup and registration for one source session."""
        key = (external_id, source)
        with self._lookup_locks_lock:
            lock = self._lookup_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._lookup_locks[key] = lock
            return lock

    # ==================== LIFECYCLE OPERATIONS ====================

    def reregister_active_sessions(self, limit: int = 1000) -> int:
        """
        Re-register active and paused sessions with the message processor.

        Called during initialization to restore message processing
        for sessions that were active before a daemon restart.  Paused
        sessions are included because their transcripts may not have
        been fully ingested before the daemon stopped.

        Args:
            limit: Maximum number of sessions to re-register per status
                   (default 1000). Sessions beyond this limit will not
                   be re-registered.

        Returns:
            Number of sessions successfully re-registered
        """
        if not self._message_processor or not self._session_manager:
            return 0

        try:
            # Query active and paused sessions from storage
            active_sessions = self._session_manager.list(status="active", limit=limit)
            paused_sessions = self._session_manager.list(status="paused", limit=limit)
            all_sessions = active_sessions + paused_sessions
            registered_count = 0

            for session in all_sessions:
                if getattr(session, "transcript_processed", False):
                    continue

                transcript_path = getattr(session, "transcript_path", None)
                if not transcript_path or transcript_path == "missing_transcript":
                    continue

                try:
                    # Determine source from session (default to claude)
                    source = getattr(session, "source", "claude") or "claude"
                    self._message_processor.register_session(
                        session.id, transcript_path, source=source
                    )
                    registered_count += 1
                except Exception as e:
                    self.logger.warning(f"Failed to re-register session {session.id}: {e}")
                    continue

            if registered_count > 0:
                self.logger.info(
                    f"Re-registered {registered_count} active/paused sessions "
                    f"with message processor"
                )

            return registered_count

        except Exception as e:
            self.logger.warning(f"Failed to re-register active/paused sessions: {e}")
            return 0

    def start_agent_run(self, agent_run_id: str) -> bool:
        """
        Mark an agent run as started when its terminal-mode session begins.

        Called from handle_session_start when a pre-created session with an
        agent_run_id is detected. This updates the status from 'pending' to
        'running' and sets the started_at timestamp.

        Args:
            agent_run_id: The agent run ID to start

        Returns:
            True if the run was started, False otherwise
        """
        if not self._agent_run_manager:
            self.logger.debug("start_agent_run: No agent_run_manager, skipping")
            return False

        try:
            agent_run = self._agent_run_manager.get(agent_run_id)
            if not agent_run:
                self.logger.warning(f"Agent run {agent_run_id} not found")
                return False

            # Only start if currently pending
            if agent_run.status != "pending":
                self.logger.debug(
                    f"Agent run {agent_run_id} not pending (status={agent_run.status}), skipping start"
                )
                return False

            self._agent_run_manager.start(agent_run_id)
            self.logger.info(f"Started agent run {agent_run_id}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to start agent run {agent_run_id}: {e}")
            return False

    def _terminate_agent_run(
        self,
        *,
        run_id: str,
        agent_run: Any,
        action: TerminalAction,
        reason: str | None,
        result_prefix: str,
        tool_calls_count: int,
        turns_used: int,
    ) -> Any | None:
        """Persist intent and capture before killing and terminalizing a run."""
        manager = self._agent_run_manager
        if manager is None:
            return None

        def terminalize(_action: TerminalAction, payload: str | None) -> Any | None:
            if _action == "complete":
                return manager.complete(
                    run_id=run_id,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                )
            return manager.fail(
                run_id=run_id,
                error=payload or reason or "Agent failed",
                tool_calls_count=tool_calls_count,
                turns_used=turns_used,
            )

        tmux_session_name = agent_run.tmux_session_name
        if not isinstance(tmux_session_name, str) or not tmux_session_name:
            if action == "complete":
                updated = manager.complete(
                    run_id=run_id,
                    result=result_prefix or None,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                )
            else:
                updated = manager.fail(
                    run_id=run_id,
                    error=reason or "Agent failed",
                    result=result_prefix or None,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                )
            return updated or manager.get(run_id)

        # Fixed tmux argv, exact session target, and shell execution disabled.
        import subprocess  # nosec B404

        from gobby.agents.tmux import get_configured_tmux_command_prefix

        target = f"={tmux_session_name}"

        def session_alive() -> bool:
            cmd = get_configured_tmux_command_prefix()
            cmd.extend(["has-session", "-t", target])
            proc = subprocess.run(  # nosec B603
                cmd,
                capture_output=True,
                timeout=5,
            )
            return proc.returncode == 0

        def capture() -> str:
            cmd = get_configured_tmux_command_prefix()
            cmd.extend(["capture-pane", "-t", target, "-p", "-S", "-"])
            proc = subprocess.run(  # nosec B603
                cmd,
                capture_output=True,
                timeout=5,
                text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or "capture-pane failed")
            return proc.stdout

        def kill() -> bool:
            cmd = get_configured_tmux_command_prefix()
            cmd.extend(["kill-session", "-t", target])
            proc = subprocess.run(  # nosec B603
                cmd,
                capture_output=True,
                timeout=5,
            )
            return proc.returncode == 0

        result = capture_then_kill_sync(
            storage=manager,
            run_id=run_id,
            session_name=tmux_session_name,
            action=action,
            reason=reason,
            result_prefix=result_prefix or None,
            session_alive=session_alive,
            capture=capture,
            kill=kill,
            terminalize=terminalize,
        )
        if not result.success:
            self.logger.warning(
                "Deferred terminalization for agent run %s: %s (%s)",
                run_id,
                result.error,
                result.error_code,
            )
            return None
        return result.run

    def complete_agent_run(self, session: Any) -> None:
        """
        Complete an agent run when its terminal-mode session ends.

        Updates the agent run status based on session outcome, removes the
        agent from the in-memory running registry, and releases any worktrees
        associated with the session.

        Args:
            session: Session object with agent_run_id
        """
        # Check for agent_run_id
        agent_run_id = getattr(session, "agent_run_id", None)
        if not agent_run_id:
            return

        self.logger.debug(f"Completing agent run {agent_run_id} for session {session.id}")

        if not self._agent_run_manager:
            return

        try:
            agent_run = self._agent_run_manager.get(agent_run_id)
            if not agent_run:
                self.logger.warning(f"Agent run {agent_run_id} not found")
                return

            # Skip DB update if already completed, but still fire completion event
            if agent_run.status not in ("pending", "running"):
                self.logger.debug(
                    f"Agent run {agent_run_id} already in terminal state: {agent_run.status}"
                )
                self._notify_agent_completion(agent_run_id, agent_run.status)
                self.release_session_worktrees(session.id)
                return

            # Use summary as result if available
            result = getattr(session, "summary_markdown", None) or ""

            # Fallback: get last assistant message if no summary available
            if not result:
                result = getattr(session, "last_assistant_content", None) or ""

            # Fallback: check inter_session_messages for send_message data
            if not result:
                try:
                    db = self._agent_run_manager.db
                    msg_row = db.fetchone(
                        """
                        SELECT content FROM inter_session_messages
                        WHERE from_session = %s
                        ORDER BY sent_at DESC
                        LIMIT 1
                        """,
                        (session.id,),
                    )
                    if msg_row:
                        result = msg_row["content"]
                        self.logger.info(f"Got result from inter_session_messages for {session.id}")
                except Exception as e:
                    self.logger.debug(
                        f"inter_session_messages fallback failed for {session.id}: {e}"
                    )

            # Flush message processor to ensure session stats are up-to-date
            # before reading them. The processor runs on a 2s poll interval, so
            # SESSION_END can fire before the final stats have been written to DB.
            session_id = session.id
            if self._message_processor:
                try:
                    if not self._event_loop or not self._event_loop.is_running():
                        raise RuntimeError("daemon event loop is not available")
                    flush_future = asyncio.run_coroutine_threadsafe(
                        self._message_processor.flush_session(session_id),
                        self._event_loop,
                    )
                    flush_future.result(timeout=5)
                except Exception as e:
                    self.logger.debug(f"Failed to flush session stats for {session_id}: {e}")

                # Re-fetch session from DB to get updated stats
                refreshed = self._session_manager.get(session_id) if self._session_manager else None
                if refreshed:
                    session = refreshed
                    result = (
                        getattr(session, "summary_markdown", None)
                        or getattr(session, "last_assistant_content", None)
                        or result
                    )

            # Count tool calls and turns from session stats
            tool_calls_count = getattr(session, "tool_call_count", 0)
            turns_used = getattr(session, "turn_count", 0)

            incomplete_workflow_error = self._incomplete_step_workflow_error(session_id)
            if incomplete_workflow_error:
                if tool_calls_count == 0 and turns_used == 0:
                    incomplete_workflow_error = (
                        f"{incomplete_workflow_error}\n\n{_format_no_activity_error(result)}"
                    )
                updated_run = self._terminate_agent_run(
                    run_id=agent_run_id,
                    agent_run=agent_run,
                    action="fail",
                    reason=incomplete_workflow_error,
                    result_prefix=result,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                )
                if updated_run is None:
                    return
                self.logger.warning(
                    f"Agent run {agent_run_id} marked as failed: "
                    f"incomplete step workflow on session end"
                )
                notification_status = self._agent_run_notification_status(
                    agent_run_id,
                    updated_run,
                    default="error",
                )
                self._notify_agent_completion(agent_run_id, notification_status)
                self.release_session_worktrees(session.id)
                return

            # Guard: agent exited cleanly but did nothing — treat as error
            if tool_calls_count == 0 and turns_used == 0:
                updated_run = self._terminate_agent_run(
                    run_id=agent_run_id,
                    agent_run=agent_run,
                    action="fail",
                    reason=_format_no_activity_error(result),
                    result_prefix=result,
                    tool_calls_count=tool_calls_count,
                    turns_used=turns_used,
                )
                if updated_run is None:
                    return
                self.logger.warning(
                    f"Agent run {agent_run_id} marked as failed: "
                    f"no activity detected (0 tool calls, 0 turns)"
                )
                notification_status = self._agent_run_notification_status(
                    agent_run_id,
                    updated_run,
                    default="error",
                )
                self._notify_agent_completion(agent_run_id, notification_status)
                self.release_session_worktrees(session.id)
                return

            # Mark as success
            updated_run = self._terminate_agent_run(
                run_id=agent_run_id,
                agent_run=agent_run,
                action="complete",
                reason=None,
                result_prefix=result,
                tool_calls_count=tool_calls_count,
                turns_used=turns_used,
            )
            if updated_run is None:
                return
            self.logger.info(
                f"Completed agent run {agent_run_id} "
                f"(tool_calls={tool_calls_count}, turns={turns_used})"
            )

            # Notify completion registry (fallback if kill_agent didn't fire it)
            notification_status = self._agent_run_notification_status(
                agent_run_id,
                updated_run,
                default="success",
            )
            self._notify_agent_completion(agent_run_id, notification_status)
            self.release_session_worktrees(session.id)

        except Exception as e:
            self.logger.error(f"Failed to complete agent run {agent_run_id}: {e}")

    def _agent_run_notification_status(
        self,
        run_id: str,
        updated_run: Any | None,
        *,
        default: str,
    ) -> str:
        """Return the persisted status when another terminalizer won the race."""
        if updated_run is not None:
            status = getattr(updated_run, "status", None)
            return status if isinstance(status, str) else default
        stored_run = self._agent_run_manager.get(run_id) if self._agent_run_manager else None
        if stored_run is None:
            self.logger.warning("Agent run %s disappeared after terminalization race", run_id)
            return default
        self.logger.debug(
            "Agent run %s terminalized concurrently with status %s",
            run_id,
            stored_run.status,
        )
        return stored_run.status

    def _incomplete_step_workflow_error(self, session_id: str) -> str | None:
        """Return a failure reason if an active step workflow is still incomplete."""
        if not self._agent_run_manager:
            return None

        db = getattr(self._agent_run_manager, "db", None)
        if db is None:
            return None

        try:
            from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
            from gobby.workflows.definitions import WorkflowDefinition
            from gobby.workflows.safe_evaluator import (
                SafeExpressionEvaluator,
                build_condition_helpers,
            )
            from gobby.workflows.state_manager import (
                SessionVariableManager,
                WorkflowInstanceManager,
            )

            instance_manager = WorkflowInstanceManager(db)
            definition_manager = LocalWorkflowDefinitionManager(db)
            session_variables = SessionVariableManager(db).get_variables(session_id)

            for instance in instance_manager.get_active_instances(session_id):
                if not instance.current_step:
                    continue

                variables = {**session_variables, **instance.variables}
                if variables.get("step_workflow_complete") is True:
                    continue

                row = definition_manager.get_by_name(instance.workflow_name)
                if not row or row.workflow_type == "pipeline":
                    continue

                definition = WorkflowDefinition(**json.loads(row.definition_json))
                if not definition.steps:
                    continue

                if not definition.exit_condition:
                    return _format_incomplete_step_workflow_error(
                        instance.workflow_name,
                        instance.current_step,
                        definition.exit_condition,
                    )

                ctx = {
                    "current_step": instance.current_step,
                    "vars": variables,
                    "variables": variables,
                }
                try:
                    exit_met = SafeExpressionEvaluator(
                        context=ctx,
                        allowed_funcs=build_condition_helpers(context=ctx),
                    ).evaluate(definition.exit_condition)
                except Exception as e:
                    return _format_incomplete_step_workflow_error(
                        instance.workflow_name,
                        instance.current_step,
                        definition.exit_condition,
                        eval_error=e,
                    )

                if not exit_met:
                    return _format_incomplete_step_workflow_error(
                        instance.workflow_name,
                        instance.current_step,
                        definition.exit_condition,
                    )
        except Exception as e:
            self.logger.warning(
                f"Failed to inspect step workflow completion for session {session_id}: {e}"
            )

        return None

    def _notify_agent_completion(self, run_id: str, status: str) -> None:
        """Fire completion event for an agent run (fail-open, idempotent).

        This may be called from a sync context (hook handler thread) where no
        event loop is running.  Uses run_coroutine_threadsafe with the stored
        main loop reference to safely schedule the async notify call.
        """
        if not self._completion_registry:
            return
        try:
            result = {"status": status, "run_id": run_id}
            coro = self._completion_registry.notify(run_id, result)

            # Prefer the current running loop (if we happen to be in async context)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(coro)
                return
            except RuntimeError:
                pass

            # Fall back to stored main event loop (cross-thread scheduling)
            if self._event_loop and not self._event_loop.is_closed():
                asyncio.run_coroutine_threadsafe(coro, self._event_loop)
            else:
                self.logger.debug(f"No event loop available to notify completion for run {run_id}")
        except Exception:
            self.logger.debug(
                f"Failed to notify completion registry for run {run_id}", exc_info=True
            )

    def release_session_worktrees(self, session_id: str) -> None:
        """
        Release all worktrees claimed by a session.

        When a session ends, any worktrees it claimed should be released
        so they can be reused by other sessions.

        Args:
            session_id: The session ID whose worktrees to release
        """
        if not self._worktree_manager:
            return

        try:
            # Find worktrees owned by this session
            worktrees = self._worktree_manager.list_worktrees(agent_session_id=session_id)

            for worktree in worktrees:
                try:
                    # Release the worktree (sets agent_session_id to NULL)
                    self._worktree_manager.release(worktree.id)
                    self.logger.debug(f"Released worktree {worktree.id} from session {session_id}")
                except Exception as e:
                    self.logger.warning(f"Failed to release worktree {worktree.id}: {e}")

            if worktrees:
                self.logger.info(f"Released {len(worktrees)} worktree(s) from session {session_id}")
        except Exception as e:
            self.logger.warning(f"Failed to list worktrees for session {session_id}: {e}")


__all__ = ["SessionCoordinator"]
