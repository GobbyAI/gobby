"""
Session coordinator module for session lifecycle management.

This module is extracted from hook_manager.py using Strangler Fig pattern.
It provides centralized session registration tracking, message caching,
and lifecycle coordination.

Classes:
    SessionCoordinator: Coordinates session lifecycle operations.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager
    from gobby.storage.sessions import LocalSessionManager
    from gobby.storage.worktrees import LocalWorktreeManager


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
        session_storage: LocalSessionManager | None = None,
        message_processor: Any | None = None,
        agent_run_manager: LocalAgentRunManager | None = None,
        worktree_manager: LocalWorktreeManager | None = None,
        logger: logging.Logger | None = None,
        completion_registry: Any | None = None,
    ) -> None:
        """
        Initialize SessionCoordinator.

        Args:
            session_storage: LocalSessionManager for session queries
            message_processor: SessionMessageProcessor for message registration
            agent_run_manager: LocalAgentRunManager for agent run completion
            worktree_manager: LocalWorktreeManager for worktree release
            logger: Optional logger instance
            completion_registry: CompletionEventRegistry for notifying on agent completion
        """
        self._session_storage = session_storage
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

        # Lock for session lookups to prevent race conditions (double firing)
        self._lookup_lock = threading.Lock()

    def set_completion_registry(self, registry: Any) -> None:
        """Inject completion registry after construction (avoids circular init ordering)."""
        self._completion_registry = registry

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

    def get_lookup_lock(self) -> threading.Lock:
        """
        Get the lookup lock for preventing race conditions.

        Returns:
            The lookup lock
        """
        return self._lookup_lock

    # ==================== LIFECYCLE OPERATIONS ====================

    def reregister_active_sessions(self, limit: int = 1000) -> int:
        """
        Re-register active sessions with the message processor.

        Called during initialization to restore message processing
        for sessions that were active before a daemon restart.

        Args:
            limit: Maximum number of sessions to re-register (default 1000).
                   Sessions beyond this limit will not be re-registered.

        Returns:
            Number of sessions successfully re-registered
        """
        if not self._message_processor or not self._session_storage:
            return 0

        try:
            # Query active sessions from storage
            active_sessions = self._session_storage.list(status="active", limit=limit)
            registered_count = 0

            for session in active_sessions:
                jsonl_path = getattr(session, "jsonl_path", None)
                if not jsonl_path:
                    continue

                try:
                    # Determine source from session (default to claude)
                    source = getattr(session, "source", "claude") or "claude"
                    self._message_processor.register_session(session.id, jsonl_path, source=source)
                    registered_count += 1
                except Exception as e:
                    self.logger.warning(f"Failed to re-register session {session.id}: {e}")

            if registered_count > 0:
                self.logger.info(
                    f"Re-registered {registered_count} active sessions with message processor"
                )

            return registered_count

        except Exception as e:
            self.logger.warning(f"Failed to re-register active sessions: {e}")
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

        # Remove from in-memory running agents registry
        # Capture tmux_session_name before removal for result fallback
        tmux_session_name: str | None = None
        try:
            from gobby.agents.registry import get_running_agent_registry

            running_registry = get_running_agent_registry()
            removed = running_registry.remove(agent_run_id)
            if removed:
                tmux_session_name = removed.tmux_session_name
                self.logger.debug(f"Unregistered running agent {agent_run_id} from registry")
        except Exception as e:
            self.logger.warning(f"Failed to unregister agent from running registry: {e}")

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
                return

            # Use summary as result if available
            result = (
                getattr(session, "summary_markdown", None)
                or getattr(session, "compact_markdown", None)
                or ""
            )

            # Fallback: get last assistant message if no summary available
            if not result:
                try:
                    db = self._agent_run_manager.db
                    msg_row = db.fetchone(
                        """
                        SELECT content FROM session_messages
                        WHERE session_id = ? AND role = 'assistant' AND tool_name IS NULL
                        ORDER BY message_index DESC
                        LIMIT 1
                        """,
                        (session.id,),
                    )
                    if msg_row:
                        result = msg_row["content"]
                except (sqlite3.Error, ValueError) as e:
                    self.logger.warning(
                        f"Failed to get last assistant message for {session.id}: {e}"
                    )

            # Fallback: check inter_session_messages for send_message data
            if not result:
                try:
                    db = self._agent_run_manager.db
                    msg_row = db.fetchone(
                        """
                        SELECT content FROM inter_session_messages
                        WHERE from_session = ?
                        ORDER BY created_at DESC
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

            # Fallback: capture terminal output from tmux session.
            # remain-on-exit is set on agent sessions so the pane persists
            # after the process exits, keeping the scrollback buffer available.
            if not result and tmux_session_name:
                try:
                    import subprocess

                    from gobby.agents.tmux.config import TmuxConfig

                    tmux_cfg = TmuxConfig()
                    cmd = [tmux_cfg.command]
                    if tmux_cfg.socket_name:
                        cmd.extend(["-L", tmux_cfg.socket_name])
                    cmd.extend(["capture-pane", "-t", tmux_session_name, "-p", "-S", "-"])

                    proc = subprocess.run(cmd, capture_output=True, timeout=5, text=True)
                    if proc.returncode == 0 and proc.stdout.strip():
                        result = proc.stdout.strip()
                        self.logger.info(
                            f"Captured result from tmux session '{tmux_session_name}' "
                            f"for agent {agent_run_id} ({len(result)} chars)"
                        )
                except Exception as e:
                    self.logger.debug(
                        f"tmux capture-pane fallback failed for {tmux_session_name}: {e}"
                    )

            # Clean up the tmux session (remain-on-exit keeps it alive for capture)
            if tmux_session_name:
                try:
                    import subprocess

                    from gobby.agents.tmux.config import TmuxConfig

                    tmux_cfg = TmuxConfig()
                    kill_cmd = [tmux_cfg.command]
                    if tmux_cfg.socket_name:
                        kill_cmd.extend(["-L", tmux_cfg.socket_name])
                    kill_cmd.extend(["kill-session", "-t", tmux_session_name])
                    subprocess.run(kill_cmd, capture_output=True, timeout=5)
                except Exception as e:
                    self.logger.debug(f"tmux kill-session failed for {tmux_session_name}: {e}")

            # Count tool calls and turns from session messages
            tool_calls_count = 0
            turns_used = 0
            try:
                db = self._agent_run_manager.db
                row = db.fetchone(
                    """
                    SELECT
                        COUNT(CASE WHEN tool_name IS NOT NULL THEN 1 END) AS tool_calls,
                        COUNT(CASE WHEN role = 'assistant' THEN 1 END) AS turns
                    FROM session_messages WHERE session_id = ?
                    """,
                    (session.id,),
                )
                if row:
                    tool_calls_count = row["tool_calls"] or 0
                    turns_used = row["turns"] or 0
            except (sqlite3.Error, ValueError) as e:
                self.logger.warning(f"Failed to count session stats for {session.id}: {e}")

            # Mark as success
            self._agent_run_manager.complete(
                run_id=agent_run_id,
                result=result,
                tool_calls_count=tool_calls_count,
                turns_used=turns_used,
            )
            self.logger.info(
                f"Completed agent run {agent_run_id} "
                f"(tool_calls={tool_calls_count}, turns={turns_used})"
            )

            # Notify completion registry (fallback if kill_agent didn't fire it)
            self._notify_agent_completion(agent_run_id, "success")

        except Exception as e:
            self.logger.error(f"Failed to complete agent run {agent_run_id}: {e}")

        # Release any worktrees associated with this session
        try:
            self.release_session_worktrees(session.id)
        except Exception as e:
            self.logger.warning(f"Failed to release worktrees for session {session.id}: {e}")

    def _notify_agent_completion(self, run_id: str, status: str) -> None:
        """Fire completion event for an agent run (fail-open, idempotent)."""
        if not self._completion_registry:
            return
        try:
            import asyncio

            result = {"status": status, "run_id": run_id}
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._completion_registry.notify(run_id, result))
            else:
                loop.run_until_complete(self._completion_registry.notify(run_id, result))
        except Exception:
            self.logger.debug(
                "Failed to notify completion registry for run %s", run_id, exc_info=True
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
