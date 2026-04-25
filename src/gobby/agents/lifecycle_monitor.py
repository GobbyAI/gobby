"""Background monitor for agent lifecycle.

Detects when agent processes die without firing SESSION_END hooks
and marks their DB records accordingly. Fully DB-driven — survives
daemon restarts without losing track of agents.

Runs as a periodic background task alongside the session lifecycle manager.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import TYPE_CHECKING, Literal

from gobby.agents.checkpoint_manager import CheckpointManager
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.kill import kill_agent
from gobby.agents.loop_tracker import LoopTracker
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier, StallStatus
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun, LocalAgentRunManager
from gobby.tasks.state_semantics import (
    is_task_actively_claimed,
)

if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.checkpoints import LocalCheckpointManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager

logger = logging.getLogger(__name__)


class AgentLifecycleMonitor:
    """Periodically checks if agent processes are still alive.

    All checks are DB-driven via agent_runs table. Survives daemon
    restarts — no in-memory registry dependency.

    When an agent dies or times out, this monitor:
    - Marks the agent_runs DB record as 'error'/'timeout'
    - Expires the agent's session
    - Recovers claimed tasks back to 'open'
    - Releases any associated worktrees/clones
    """

    def __init__(
        self,
        agent_run_manager: LocalAgentRunManager,
        db: DatabaseProtocol,
        session_manager: SessionManager | None = None,
        session_coordinator: SessionCoordinator | None = None,
        clone_storage: LocalCloneManager | None = None,
        check_interval_seconds: float = 30.0,
        completion_registry: CompletionEventRegistry | None = None,
        task_manager: LocalTaskManager | None = None,
        tmux_config: TmuxConfig | None = None,
        checkpoint_storage: LocalCheckpointManager | None = None,
        worktree_storage: LocalWorktreeManager | None = None,
        project_manager: LocalProjectManager | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
        self._session_manager = session_manager
        self._session_coordinator = session_coordinator
        self._clone_storage = clone_storage
        self._check_interval = check_interval_seconds
        self._completion_registry = completion_registry
        self._task_manager = task_manager
        self._tmux_config = tmux_config or TmuxConfig()
        self._tmux = TmuxSessionManager(config=self._tmux_config)
        self._idle_detector = IdleDetector()
        self._prompt_detector = PromptDetector()
        self._stall_classifier = StallClassifier()
        self._loop_tracker = LoopTracker(threshold=3)
        self._checkpoint_manager = (
            CheckpointManager(checkpoint_storage) if checkpoint_storage else None
        )
        self._worktree_storage = worktree_storage
        self._project_manager = project_manager
        self._running = False
        self._task: asyncio.Task[None] | None = None
        # In-memory tracking for inherently non-persistable state
        self._master_fds: dict[str, int] = {}

    def set_session_coordinator(self, coordinator: SessionCoordinator) -> None:
        """Inject session coordinator after construction (avoids circular init ordering)."""
        self._session_coordinator = coordinator

    def register_master_fd(self, run_id: str, fd: int) -> None:
        """Register a PTY master file descriptor for an agent."""
        self._master_fds[run_id] = fd

    async def _resolve_claimed_task_for_run(self, db_run: AgentRun) -> tuple[str, object] | None:
        """Resolve the task still owned by this run, if any."""
        if not self._task_manager:
            return None

        task_id = db_run.task_id

        if not task_id and db_run.child_session_id:
            tasks = await asyncio.to_thread(
                self._task_manager.list_tasks,
                claimed_by_session_id=db_run.child_session_id,
                closed=False,
            )
            if tasks:
                task_id = tasks[0].id

        if not task_id:
            return None

        task = await asyncio.to_thread(self._task_manager.get_task, task_id)
        expected_owner = db_run.child_session_id or db_run.claimed_session_id
        if not task or not is_task_actively_claimed(task, expected_owner):
            return None

        return task_id, task

    async def _recover_task_from_terminal_agent(
        self,
        db_run: AgentRun,
        *,
        outcome: Literal["failed", "cancelled"],
    ) -> None:
        """Recover task ownership after a failed or cancelled agent run."""
        if not self._task_manager:
            return
        try:
            resolved = await self._resolve_claimed_task_for_run(db_run)
            if resolved is None:
                return

            task_id, task = resolved
            task_ref = f"#{task.seq_num}" if task.seq_num else task_id[:8]
            raw_stage = getattr(task, "lifecycle_stage", None)
            raw_status = getattr(task, "status", None)
            lifecycle_stage = raw_stage if isinstance(raw_stage, str) and raw_stage else None
            if lifecycle_stage is None and isinstance(raw_status, str) and raw_status:
                lifecycle_stage = raw_status

            if outcome == "cancelled":
                release_kwargs: dict[str, object] = {}
                if lifecycle_stage == "in_progress":
                    release_kwargs["status"] = "open"
                await asyncio.to_thread(
                    self._task_manager.release_task_claim,
                    task_id,
                    **release_kwargs,
                )
                logger.info(
                    "Recovered task %s after agent %s cancelled (status=%s)",
                    task_ref,
                    db_run.id,
                    task.status,
                )
                return

            is_provider = self._stall_classifier.is_provider_error(db_run.error)
            if is_provider:
                logger.info(
                    "Agent %s failed with provider error (provider=%s): %s",
                    db_run.id,
                    db_run.provider,
                    db_run.error,
                )

            if lifecycle_stage != "in_progress":
                await asyncio.to_thread(self._task_manager.release_task_claim, task_id)
                logger.info(
                    "Released stale ownership on task %s after agent %s failed (status=%s)",
                    task_ref,
                    db_run.id,
                    task.status,
                )
                return

            failure_count = task.dispatch_failure_count or 0
            if not is_provider:
                failure_count += 1

            if not is_provider and failure_count >= 3:
                await asyncio.to_thread(
                    self._task_manager.release_task_claim,
                    task_id,
                    status="escalated",
                    dispatch_failure_count=0,
                    escalation_reason=f"Failed {failure_count} times across different agents",
                )
                logger.warning(
                    "Task %s escalated: %s failures across different agents",
                    task_ref,
                    failure_count,
                )
                return

            await asyncio.to_thread(
                self._task_manager.release_task_claim,
                task_id,
                status="open",
                dispatch_failure_count=failure_count,
            )
            logger.info(f"Recovered task {task_ref} to open after agent {db_run.id} failed")
        except Exception as e:
            logger.warning(f"Failed to recover task for agent {db_run.id}: {e}")

    async def _recover_task_from_failed_agent(self, run_id: str) -> None:
        """Recover task ownership after a failed agent run."""
        db_run = await asyncio.to_thread(self._agent_run_manager.get, run_id)
        if not db_run:
            return
        await self._recover_task_from_terminal_agent(db_run, outcome="failed")

    async def _notify_terminal_completion(
        self,
        run_id: str,
        *,
        result: dict[str, str],
        message: str,
    ) -> None:
        """Notify waiters about a terminal run transition."""
        if not self._completion_registry:
            return
        try:
            await self._completion_registry.notify(run_id, result=result, message=message)
        except Exception as e:
            logger.warning(f"Failed to notify completion for {run_id}: {e}")

    async def _post_terminal_cleanup(self, run: AgentRun) -> None:
        """Release in-memory and isolation state for a terminal agent run."""
        session_id = run.child_session_id or run.parent_session_id

        fd = self._master_fds.pop(run.id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        self._prompt_detector.clear(run.id)
        self._stall_classifier.clear(run.id)
        self._loop_tracker.clear(run.id)

        if self._session_coordinator and session_id:
            try:
                self._session_coordinator.release_session_worktrees(session_id)
            except Exception as e:
                logger.warning(f"Failed to release worktrees for agent {run.id}: {e}")

        if self._clone_storage and run.clone_id:
            try:
                await asyncio.to_thread(self._clone_storage.release, run.clone_id)
            except Exception as e:
                logger.warning(f"Failed to release clone for agent {run.id}: {e}")

        if self._session_manager and session_id:
            try:
                await asyncio.to_thread(self._session_manager.update_status, session_id, "expired")
                logger.debug(f"Expired session {session_id} for agent {run.id}")
            except Exception as e:
                logger.warning(f"Failed to expire session for agent {run.id}: {e}")

    async def terminalize_cancelled_run(self, run_id: str, *, terminal_reason: str) -> bool:
        """Mark an active run cancelled, recover ownership, and notify waiters."""
        db_run = await asyncio.to_thread(
            self._agent_run_manager.cancel,
            run_id,
            terminal_reason=terminal_reason,
        )
        if db_run is None:
            current = await asyncio.to_thread(self._agent_run_manager.get, run_id)
            logger.debug(
                "Cancelled terminalization no-op for run %s; current status=%s",
                run_id,
                current.status if current else "missing",
            )
            return False

        await self._recover_task_from_terminal_agent(db_run, outcome="cancelled")
        await self._notify_terminal_completion(
            db_run.id,
            result={
                "status": "cancelled",
                "terminal_reason": terminal_reason,
                "run_id": db_run.id,
            },
            message=f"Agent {db_run.id} cancelled",
        )
        await self._post_terminal_cleanup(db_run)
        return True

    async def start(self) -> None:
        """Start the monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._check_loop(),
            name="agent-lifecycle-monitor",
        )
        logger.info(f"AgentLifecycleMonitor started (interval={self._check_interval}s)")

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("AgentLifecycleMonitor stopped")

    async def _check_loop(self) -> None:
        """Periodic check loop."""
        # Brief initial delay to let agents finish spawning on startup
        await asyncio.sleep(5.0)

        iteration = 0
        while self._running:
            try:
                logger.debug(f"Lifecycle check iteration {iteration}")
                await self.check_trust_prompts()  # Fast unblock before other checks
                await self.check_loop_prompts()  # Dismiss loop detection prompts
                await self.check_unhealthy_agents()
                await self.expire_terminal_run_sessions()
                await self.check_initialization_timeout()
                await self.check_idle_agents()
                await self.check_provider_stalls()

                # DB-driven stale run cleanup every 10th iteration.
                # Uses per-agent timeout_seconds and expires sessions.
                if iteration > 0 and iteration % 10 == 0:
                    try:
                        cleaned = await asyncio.to_thread(
                            self._agent_run_manager.cleanup_stale_runs
                        )
                        if cleaned:
                            logger.info(f"Cleaned up {cleaned} stale agent runs")
                    except Exception as e:
                        logger.warning(f"Stale run cleanup failed: {e}")

                iteration += 1
            except Exception as e:
                logger.error(f"Agent lifecycle check error: {e}")

            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break

    def _get_active_terminal_runs(self) -> list[AgentRun]:
        """Get active terminal agent runs with tmux sessions from DB."""
        runs = self._agent_run_manager.list_active()
        return [r for r in runs if r.tmux_session_name]

    async def expire_terminal_run_sessions(self) -> int:
        """Expire sessions whose agent run is already in a terminal state."""
        expired = await asyncio.to_thread(self._agent_run_manager.expire_sessions_for_terminal_runs)
        if expired:
            logger.info("Expired %s session(s) for terminal agent runs", expired)
        return expired

    async def check_trust_prompts(self) -> int:
        """Check for folder trust prompts and auto-dismiss them.

        Sends Enter to accept "Trust Folder" and dismiss the prompt.
        Only fires once per agent to avoid repeated key-sends.

        Returns:
            Number of trust prompts dismissed.
        """
        runs = await asyncio.to_thread(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            if self._prompt_detector.was_dismissed(run.id):
                continue

            tmux_name = run.tmux_session_name
            assert tmux_name is not None  # guaranteed by filter

            try:
                pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
                if pane_output and self._prompt_detector.detect_trust_prompt(pane_output):
                    sent = await self._tmux.send_keys(tmux_name, PromptDetector.TRUST_DISMISS_KEYS)
                    if sent:
                        self._prompt_detector.mark_dismissed(run.id)
                        logger.info(
                            f"Auto-dismissed trust prompt for agent {run.id} (trust parent folder)",
                        )
                        handled += 1
            except Exception as e:
                logger.warning(f"Error checking trust prompt for agent {run.id}: {e}")

        return handled

    async def check_loop_prompts(self) -> int:
        """Check for loop detection prompts and auto-dismiss them.

        Gemini CLI detects when agents appear stuck in a loop and shows
        a confirmation prompt. This sends "y" to continue execution.
        Unlike trust prompts, loop detection can fire multiple times
        per session so there is no dismissed tracking.

        Returns:
            Number of loop prompts dismissed.
        """
        runs = await asyncio.to_thread(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            tmux_name = run.tmux_session_name
            assert tmux_name is not None  # guaranteed by filter

            try:
                pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
                if pane_output and self._prompt_detector.detect_loop_prompt(pane_output):
                    count = self._loop_tracker.record_dismissal(run.id)

                    if self._loop_tracker.should_escalate(run.id):
                        logger.warning(
                            f"Doom loop detected for agent {run.id}: "
                            f"{count} loop prompts dismissed, escalating to kill"
                        )
                        await self._checkpoint_and_kill_looping_agent(run)
                    else:
                        sent = await self._tmux.send_keys(
                            tmux_name, PromptDetector.LOOP_DISMISS_KEYS
                        )
                        if sent:
                            logger.info(
                                f"Auto-dismissed loop prompt for agent {run.id} "
                                f"({count}/{self._loop_tracker.threshold})"
                            )
                            handled += 1
            except Exception as e:
                logger.warning(f"Error checking loop prompt for agent {run.id}: {e}")

        return handled

    async def _cleanup_agent(
        self,
        run: AgentRun,
        error: str,
        is_success: bool = False,
        is_timeout: bool = False,
    ) -> None:
        """Full cleanup chain for an agent that needs cleanup.

        Handles DB record, task recovery, completion notification,
        in-memory state cleanup, detector state, isolation release,
        and session expiration.

        Args:
            run: The agent run DB record.
            error: Error message or completion reason.
            is_success: If True, mark as completed (not failed) and skip task recovery.
            is_timeout: If True, mark as timed out instead of failed.
        """
        terminal_run = run
        transitioned = False

        if run.status in ("pending", "running"):
            if is_success:
                updated = await asyncio.to_thread(
                    self._agent_run_manager.complete,
                    run.id,
                    result=error,  # "error" is really "reason" for success case
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
            elif is_timeout:
                updated = await asyncio.to_thread(
                    self._agent_run_manager.timeout,
                    run.id,
                    error=error,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
                    logger.info(f"Marked agent run {run.id} as timed out: {error}")
            else:
                updated = await asyncio.to_thread(
                    self._agent_run_manager.fail,
                    run.id,
                    error=error,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
                    logger.info(f"Marked agent run {run.id} as failed: {error}")

        if transitioned:
            if not is_success:
                await self._recover_task_from_terminal_agent(terminal_run, outcome="failed")

            if is_success:
                result_data: dict[str, str] = {"status": "completed"}
            else:
                result_data = {"status": "error", "error": error}
            await self._notify_terminal_completion(
                run.id,
                result=result_data,
                message=f"Agent {run.id} {'completed' if is_success else 'failed'}",
            )
        else:
            current = await asyncio.to_thread(self._agent_run_manager.get, run.id)
            logger.debug(
                "Terminal cleanup no-op for run %s; current status=%s",
                run.id,
                current.status if current else "missing",
            )
            if current is not None:
                terminal_run = current

        await self._post_terminal_cleanup(terminal_run)

    async def check_unhealthy_agents(self) -> int:
        """Detect and clean up dead or expired agents.

        Fully DB-driven — queries agent_runs table directly.

        Handles three cases:
        1. Expired agents (any mode): exceeded timeout — killed and cleaned up
        2. Dead terminal agents: tmux session or process died — cleaned up
        3. Dead autonomous agents: asyncio.Task completed or failed — cleaned up

        Returns:
            Number of agents cleaned up.
        """
        from datetime import UTC, datetime

        runs = await asyncio.to_thread(self._agent_run_manager.list_active)
        now = datetime.now(UTC)
        cleaned = 0

        for run in runs:
            try:
                # --- Detection ---
                reason: str | None = None
                is_success = False
                is_timeout = False

                # Check timeout first (applies to all agent types)
                if run.timeout_seconds and run.started_at:
                    started = datetime.fromisoformat(run.started_at)
                    age = (now - started).total_seconds()
                    if age > run.timeout_seconds:
                        reason = f"Agent exceeded {run.timeout_seconds}s timeout"
                        is_timeout = True
                        logger.info(
                            f"Agent {run.id} exceeded timeout ({age:.1f}s > {run.timeout_seconds}s)"
                        )

                # Terminal agents: check if tmux/process died
                if reason is None and run.tmux_session_name:
                    tmux_alive = await self._tmux.has_session(run.tmux_session_name)
                    if tmux_alive:
                        if run.pid:
                            try:
                                os.kill(run.pid, 0)
                            except ProcessLookupError:
                                reason = (
                                    f"PID {run.pid} dead but tmux '{run.tmux_session_name}' alive"
                                )
                                logger.info(f"Agent {run.id} {reason} - cleaning up")
                            except PermissionError:
                                pass  # Process exists but we can't signal it
                    else:
                        reason = "tmux session died unexpectedly"
                        logger.info(
                            f"Detected dead tmux session '{run.tmux_session_name}' "
                            f"for agent {run.id}"
                        )

                if reason is None:
                    continue

                # --- Capture diagnostics before kill ---
                pane_snapshot = ""
                if run.tmux_session_name and not is_success:
                    try:
                        pane_snapshot = (
                            await self._tmux.capture_pane(run.tmux_session_name, lines=50) or ""
                        )
                    except Exception as e:
                        logger.debug(f"Failed to capture pane for agent {run.id}: {e}")

                # --- Kill process ---
                if run.tmux_session_name:
                    await kill_agent(
                        run,
                        self._db,
                        signal_name="TERM",
                        timeout=5.0,
                        close_terminal=True,
                    )
                elif run.pid:
                    try:
                        os.kill(run.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass  # Already dead
                    except Exception as e:
                        logger.warning(f"Failed to kill process {run.pid}: {e}")

                # --- Build error message with diagnostics ---
                error_msg = reason
                if pane_snapshot:
                    error_msg += f"\n\n--- Last terminal output ---\n{pane_snapshot[-2000:]}"

                # --- Full cleanup chain ---
                await self._cleanup_agent(
                    run,
                    error=error_msg,
                    is_success=is_success,
                    is_timeout=is_timeout,
                )
                cleaned += 1

            except Exception as e:
                logger.warning(f"Error checking agent {run.id}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} unhealthy agent(s)")

        return cleaned

    async def check_idle_agents(self) -> int:
        """Check for idle agents and reprompt or fail them.

        Returns:
            Number of agents reprompted or failed.
        """
        if not self._tmux_config.idle_check_enabled:
            return 0

        runs = await asyncio.to_thread(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            try:
                handled += await self._handle_idle_check(run)
            except Exception as e:
                logger.warning(f"Error checking idle state for agent {run.id}: {e}")

        return handled

    async def check_initialization_timeout(self) -> int:
        """Detect agents that never initialized (provider hung on connect).

        If an agent has been running for > init_timeout_seconds and its
        session was never updated (updated_at ≈ created_at), it likely
        never got past the provider API call. Kill it with a provider-error
        error message so rotation kicks in on re-dispatch.

        Returns:
            Number of agents killed.
        """
        from datetime import UTC, datetime

        runs = await asyncio.to_thread(self._agent_run_manager.list_active)
        now = datetime.now(UTC)
        killed = 0

        for run in runs:
            if not run.started_at:
                continue
            try:
                started = datetime.fromisoformat(run.started_at)
                age = (now - started).total_seconds()
                if age < self._tmux_config.init_timeout_seconds:
                    continue

                # Check if session was ever updated
                session_id = run.child_session_id or run.parent_session_id
                if not session_id or not self._session_manager:
                    continue

                session = await asyncio.to_thread(self._session_manager.get, session_id)
                if not session or not session.updated_at or not session.created_at:
                    continue

                updated = datetime.fromisoformat(session.updated_at)
                created = datetime.fromisoformat(session.created_at)
                if (updated - created).total_seconds() > 5.0:
                    continue  # Session was updated — agent initialized fine

                # Agent never initialized. Kill it.
                logger.warning(
                    f"Agent {run.id} never initialized after {age:.0f}s "
                    f"(provider={run.provider}) — killing for provider rotation"
                )
                if run.tmux_session_name:
                    await self._tmux.kill_session(run.tmux_session_name)
                elif run.pid:
                    try:
                        os.kill(run.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

                error_msg = (
                    f"Provider connection timed out: agent never initialized "
                    f"after {age:.0f}s (provider={run.provider})"
                )
                await self._cleanup_agent(run, error=error_msg, is_success=False)
                killed += 1

            except Exception as e:
                logger.warning(f"Error checking init timeout for agent {run.id}: {e}")

        if killed > 0:
            logger.info(f"Killed {killed} uninitialized agent(s) for provider rotation")

        return killed

    async def check_provider_stalls(self) -> int:
        """Check tmux agents for provider-side stalls (rate limits, outages).

        When a stall is confirmed (2+ consecutive checks showing provider
        errors), kills the agent and triggers the full cleanup chain so
        provider rotation can kick in on re-dispatch.

        Returns:
            Number of agents killed due to provider stalls.
        """
        runs = await asyncio.to_thread(self._get_active_terminal_runs)

        stalled = 0
        for run in runs:
            try:
                tmux_name = run.tmux_session_name
                assert tmux_name is not None

                # Only capture last 8 lines — provider errors appear at the
                # bottom of the pane. 30 lines would include the agent's own
                # working output (code, task descriptions) which can contain
                # false-positive text like "rate limit" in variable names.
                pane_output = await self._tmux.capture_pane(tmux_name, lines=8)
                classification = self._stall_classifier.classify(
                    run.id,
                    pane_output=pane_output,
                )

                if classification.status == StallStatus.PROVIDER_STALL:
                    logger.warning(
                        f"Provider stall confirmed for agent {run.id}: "
                        f"{classification.reason} "
                        f"(consecutive={classification.consecutive_hits}) — killing agent",
                    )

                    # Kill the agent process
                    if run.tmux_session_name:
                        await self._tmux.kill_session(run.tmux_session_name)

                    # Error message must match provider error patterns so
                    # _recover_task_from_failed_agent classifies it correctly
                    error_msg = (
                        f"Provider stall: {classification.reason} "
                        f"(provider={run.provider}, "
                        f"consecutive_hits={classification.consecutive_hits})"
                    )
                    await self._cleanup_agent(run, error=error_msg, is_success=False)
                    stalled += 1
            except Exception as e:
                logger.warning(f"Error checking provider stall for agent {run.id}: {e}")

        return stalled

    async def _handle_idle_check(self, run: AgentRun) -> int:
        """Handle idle check for a single agent. Returns 1 if action taken, 0 otherwise.

        Uses session updated_at as the primary idle signal.  If the session
        was recently active (within idle_timeout_seconds), the agent is
        considered active regardless of what the tmux pane shows.

        When the session is stale, the agent is considered idle.  Pane
        pattern matching is only used to detect specific actionable
        conditions (context_full) that require immediate failure rather
        than the standard reprompt flow.
        """
        latest_run = await asyncio.to_thread(self._agent_run_manager.get, run.id)
        if latest_run is None or latest_run.status not in ("pending", "running"):
            self._idle_detector.reset_idle(run.id)
            return 0

        run = latest_run
        tmux_name = run.tmux_session_name
        assert tmux_name is not None

        # --- Primary signal: session updated_at ---
        session_stale = False
        session_id = run.child_session_id or run.parent_session_id
        if session_id and self._session_manager:
            session = await asyncio.to_thread(self._session_manager.get, session_id)
            if session and session.updated_at:
                from datetime import UTC, datetime

                try:
                    last_update = datetime.fromisoformat(session.updated_at)
                    elapsed = (datetime.now(UTC) - last_update).total_seconds()
                    if elapsed < self._tmux_config.idle_timeout_seconds:
                        # Session has recent activity — agent is working
                        self._idle_detector.reset_idle(run.id)
                        return 0
                    else:
                        session_stale = True
                except (ValueError, TypeError):
                    pass  # Fall through to pane-based detection

        # --- Secondary: pane patterns for specific actionable signals ---
        pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
        if pane_output is None:
            if session_stale:
                # Session is stale but can't read pane — treat as idle
                pass
            else:
                return 0

        if pane_output is not None:
            status = self._idle_detector.detect(pane_output)

            if status == "context_full":
                logger.info(f"Agent {run.id} hit context window limit - failing")
                await self._fail_idle_agent(run, reason="context window exhausted")
                return 1

            # If session_stale is set, the agent is idle regardless of pane content.
            # Pane "active" does NOT override a stale session — the session timestamp
            # is the authoritative signal.
            if not session_stale and status == "active":
                self._idle_detector.reset_idle(run.id)
                return 0

        # Agent is idle (session stale, or pane shows idle/stalled prompt)
        if self._idle_detector.should_fail(run.id, self._tmux_config.max_reprompt_attempts):
            logger.info(
                f"Agent {run.id} still idle after "
                f"{self._tmux_config.max_reprompt_attempts} reprompts — failing"
            )
            await self._fail_idle_agent(run, reason="idle after max reprompt attempts")
            return 1

        if self._idle_detector.should_reprompt(
            run.id,
            self._tmux_config.idle_timeout_seconds,
            self._tmux_config.max_reprompt_attempts,
        ):
            logger.info(f"Reprompting idle agent {run.id}")
            sent = await self._tmux.send_keys(tmux_name, IdleDetector.REPROMPT_MESSAGE + "\n")
            if sent:
                self._idle_detector.record_reprompt(run.id)
            return 1

        return 0

    async def _checkpoint_and_kill_looping_agent(self, run: AgentRun) -> None:
        """Checkpoint work, kill tmux, then full cleanup for a doom-looping agent.

        Follows the same pattern as _fail_idle_agent but adds a checkpoint
        step to preserve uncommitted work before killing.
        """
        # 1. Checkpoint uncommitted work
        if self._checkpoint_manager and run.task_id:
            cwd = await self._resolve_agent_cwd(run)
            if cwd:
                try:
                    checkpoint = await asyncio.to_thread(
                        self._checkpoint_manager.create_checkpoint,
                        cwd,
                        run.task_id,
                        run.child_session_id or run.parent_session_id,
                        run.id,
                    )
                    if checkpoint:
                        logger.info(
                            f"Checkpointed agent {run.id} work: {checkpoint.ref_name} "
                            f"({checkpoint.files_changed} files)"
                        )
                except Exception as e:
                    logger.warning(f"Failed to checkpoint agent {run.id}: {e}")

        # 2. Kill tmux session
        if run.tmux_session_name:
            await self._tmux.kill_session(run.tmux_session_name)

        # 3. Full cleanup chain
        threshold = self._loop_tracker.threshold
        await self._cleanup_agent(
            run, error=f"doom loop: dismissed loop prompt {threshold}+ times", is_success=False
        )

    async def _resolve_agent_cwd(self, run: AgentRun) -> str | None:
        """Resolve the working directory for an agent run.

        Checks worktree, clone, then session project path as fallbacks.
        """
        # Check worktree
        if run.worktree_id and self._worktree_storage:
            try:
                wt = await asyncio.to_thread(self._worktree_storage.get, run.worktree_id)
                if wt and wt.worktree_path:
                    return wt.worktree_path
            except Exception:
                logger.debug(
                    f"Failed to resolve worktree {run.worktree_id} for run {run.id}", exc_info=True
                )

        # Check clone
        if run.clone_id and self._clone_storage:
            try:
                clone = await asyncio.to_thread(self._clone_storage.get, run.clone_id)
                if clone and clone.clone_path:
                    return clone.clone_path
            except Exception:
                logger.debug(
                    f"Failed to resolve clone {run.clone_id} for run {run.id}", exc_info=True
                )

        # Fallback: session's project directory via project_id → projects.repo_path
        if run.child_session_id and self._session_manager:
            try:
                session = await asyncio.to_thread(self._session_manager.get, run.child_session_id)
                if session and session.project_id:
                    pm = self._project_manager
                    if pm is None:
                        from gobby.storage.projects import LocalProjectManager

                        pm = LocalProjectManager(self._db)
                        self._project_manager = pm
                    project = await asyncio.to_thread(pm.get, session.project_id)
                    if project and project.repo_path:
                        return str(project.repo_path)
            except Exception:
                logger.debug(
                    f"Failed to resolve project path for session {run.child_session_id}",
                    exc_info=True,
                )

        return None

    async def _fail_idle_agent(self, run: AgentRun, reason: str) -> None:
        """Fail an agent that is irrecoverably idle.

        Uses _cleanup_agent for the full chain, but kills tmux and clears
        idle state first.
        """
        # Kill tmux session before cleanup
        if run.tmux_session_name:
            await self._tmux.kill_session(run.tmux_session_name)

        # Clear idle-specific state
        self._idle_detector.clear_state(run.id)

        # Full cleanup chain (handles DB, task recovery, completion, session expiry)
        await self._cleanup_agent(run, error=f"Agent idle: {reason}", is_success=False)

    async def cleanup_stale_pending_runs(self) -> int:
        """Clean up agent runs stuck in pending status after daemon restart.

        Returns:
            Number of stale pending runs cleaned up.
        """
        return await asyncio.to_thread(self._agent_run_manager.cleanup_stale_pending_runs)
