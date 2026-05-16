"""Background monitor for agent lifecycle.

Detects when agent processes die without firing SESSION_END hooks
and marks their DB records accordingly. Fully DB-driven — survives
daemon restarts without losing track of agents.

Runs as a periodic background task alongside the session lifecycle manager.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.agent_cleanup import AgentCleanupHandler
from gobby.agents.agent_health import AgentHealthMonitor
from gobby.agents.checkpoint_manager import CheckpointManager
from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.kill import kill_agent as kill_agent  # Re-imported for tests
from gobby.agents.loop_tracker import LoopTracker
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier
from gobby.agents.task_recovery import TaskRecoveryHandler
from gobby.agents.terminal_prompt_monitor import TerminalPromptMonitor
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig

if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.agents import AgentRun, AgentRunTerminalReason, LocalAgentRunManager
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
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
        self._run_db = run_db
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
        # In-memory tracking for inherently non-persistable state
        self._master_fds: dict[str, int] = {}

        # Handlers
        self._task_recovery = TaskRecoveryHandler(
            task_manager=task_manager,
            agent_run_manager=agent_run_manager,
            stall_classifier=self._stall_classifier,
            run_db=run_db,
        )
        self._terminal_prompt_monitor = TerminalPromptMonitor(
            get_active_terminal_runs=self._get_active_terminal_runs,
            get_tmux=lambda: self._tmux,
            prompt_detector=self._prompt_detector,
            loop_tracker=self._loop_tracker,
            get_tmux_config=lambda: self._tmux_config,
            handle_looping_agent=lambda run: self._checkpoint_and_kill_looping_agent(run),
            run_db=run_db,
        )
        self._cleanup_handler = AgentCleanupHandler(
            agent_run_manager=agent_run_manager,
            db=db,
            get_session_manager=lambda: self._session_manager,
            get_session_coordinator=lambda: self._session_coordinator,
            clone_storage=clone_storage,
            completion_registry=completion_registry,
            task_recovery=self._task_recovery,
            prompt_detector=self._prompt_detector,
            terminal_prompt_monitor=self._terminal_prompt_monitor,
            stall_classifier=self._stall_classifier,
            loop_tracker=self._loop_tracker,
            master_fds=self._master_fds,
            run_db=run_db,
        )
        self._health_monitor = AgentHealthMonitor(
            agent_run_manager=agent_run_manager,
            db=db,
            tmux=self._tmux,
            get_session_manager=lambda: self._session_manager,
            stall_classifier=self._stall_classifier,
            cleanup_handler=self._cleanup_handler,
            tmux_config=self._tmux_config,
            run_db=run_db,
        )
        self._idle_check_handler = IdleCheckHandler(
            agent_run_manager=agent_run_manager,
            get_session_manager=lambda: self._session_manager,
            tmux=self._tmux,
            idle_detector=self._idle_detector,
            cleanup_handler=self._cleanup_handler,
            tmux_config=self._tmux_config,
            run_db=run_db,
        )

        self._checkpoint_manager = (
            CheckpointManager(checkpoint_storage) if checkpoint_storage else None
        )
        self._worktree_storage = worktree_storage
        self._project_manager = project_manager
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def _run_sqlite(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    def set_session_coordinator(self, coordinator: SessionCoordinator) -> None:
        """Inject session coordinator after construction (avoids circular init ordering)."""
        self._session_coordinator = coordinator

    def register_master_fd(self, run_id: str, fd: int) -> None:
        """Register a PTY master file descriptor for an agent."""
        self._master_fds[run_id] = fd

    def get_cleanup_agent(self) -> Callable[..., Awaitable[None]] | None:
        """Return the cleanup callable used for restart reconciliation."""
        return self._cleanup_handler.cleanup_agent

    async def terminalize_cancelled_run(
        self,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason,
    ) -> bool:
        """Mark an active run cancelled, recover ownership, and notify waiters."""
        return await self._cleanup_handler.terminalize_cancelled_run(
            run_id, terminal_reason=terminal_reason
        )

    async def terminalize_successful_run(
        self,
        run_id: str,
        *,
        notify_result: dict[str, Any],
        message: str,
        completion_result: str | None = None,
    ) -> bool:
        """Mark an active run successful, notify waiters, and clean child-owned state."""
        return await self._cleanup_handler.terminalize_successful_run(
            run_id,
            notify_result=notify_result,
            message=message,
            completion_result=completion_result,
        )

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
                await self.check_trust_prompts()
                await self.check_loop_prompts()
                await self.check_approval_prompts()
                await self.check_periodic_enters()
                await self.check_unhealthy_agents()
                await self.expire_terminal_run_sessions()
                await self.check_initialization_timeout()
                await self.check_idle_agents()
                await self.check_provider_stalls()

                if iteration > 0 and iteration % 10 == 0:
                    try:
                        cleaned = await self._run_sqlite(self._agent_run_manager.cleanup_stale_runs)
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
        return await self._cleanup_handler.expire_terminal_run_sessions()

    async def check_trust_prompts(self) -> int:
        """Check for folder trust prompts and auto-dismiss them."""
        return await self._terminal_prompt_monitor.check_trust_prompts()

    async def check_loop_prompts(self) -> int:
        """Check for loop detection prompts and auto-dismiss them."""
        return await self._terminal_prompt_monitor.check_loop_prompts()

    async def check_approval_prompts(self) -> int:
        """Check for approval prompts and send Enter when explicitly permitted."""
        return await self._terminal_prompt_monitor.check_approval_prompts()

    async def check_periodic_enters(self) -> int:
        """Periodically send Enter to active autonomous terminal agents."""
        return await self._terminal_prompt_monitor.check_periodic_enters()

    async def check_unhealthy_agents(self) -> int:
        """Detect and clean up dead or expired agents."""
        return await self._health_monitor.check_unhealthy_agents()

    async def check_idle_agents(self) -> int:
        """Check for idle agents and reprompt or fail them."""
        return await self._idle_check_handler.check_idle_agents()

    async def check_initialization_timeout(self) -> int:
        """Detect agents that never initialized (provider hung on connect)."""
        return await self._health_monitor.check_initialization_timeout()

    async def check_provider_stalls(self) -> int:
        """Check tmux agents for provider-side stalls (rate limits, outages)."""
        return await self._health_monitor.check_provider_stalls()

    async def _checkpoint_and_kill_looping_agent(self, run: AgentRun) -> None:
        """Checkpoint work, kill tmux, then full cleanup for a doom-looping agent."""
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

        if run.tmux_session_name:
            await self._tmux.kill_session(run.tmux_session_name)

        threshold = self._loop_tracker.threshold
        await self._cleanup_handler.cleanup_agent(
            run,
            terminal_payload=f"doom loop: dismissed loop prompt {threshold}+ times",
        )

    async def _resolve_agent_cwd(self, run: AgentRun) -> str | None:
        """Resolve the working directory for an agent run."""
        if run.worktree_id and self._worktree_storage:
            try:
                wt = await self._run_sqlite(self._worktree_storage.get, run.worktree_id)
                if wt and wt.worktree_path:
                    return cast(str, wt.worktree_path)
            except Exception:
                logger.debug(
                    f"Failed to resolve worktree {run.worktree_id} for run {run.id}", exc_info=True
                )

        if run.clone_id and self._clone_storage:
            try:
                clone = await self._run_sqlite(self._clone_storage.get, run.clone_id)
                if clone and clone.clone_path:
                    return cast(str, clone.clone_path)
            except Exception:
                logger.debug(
                    f"Failed to resolve clone {run.clone_id} for run {run.id}", exc_info=True
                )

        if run.child_session_id and self._session_manager:
            try:
                session = await self._run_sqlite(self._session_manager.get, run.child_session_id)
                if session and session.project_id:
                    pm = self._project_manager
                    if pm is None:
                        from gobby.storage.projects import LocalProjectManager

                        pm = LocalProjectManager(self._db)
                        self._project_manager = pm
                    project = await self._run_sqlite(pm.get, session.project_id)
                    if project and project.repo_path:
                        return str(project.repo_path)
            except Exception:
                logger.debug(
                    f"Failed to resolve project path for session {run.child_session_id}",
                    exc_info=True,
                )

        return None

    async def cleanup_stale_pending_runs(self) -> int:
        """Clean up agent runs stuck in pending status after daemon restart."""
        return await self._cleanup_handler.cleanup_stale_pending_runs()

    # Delegates for backward compatibility and test stability
    async def _cleanup_agent(self, *args: Any, **kwargs: Any) -> None:
        """Delegate to cleanup handler."""
        await self._cleanup_handler.cleanup_agent(*args, **kwargs)

    async def _recover_task_from_failed_agent(self, *args: Any, **kwargs: Any) -> None:
        """Delegate to task recovery handler."""
        await self._task_recovery.recover_task_from_failed_agent(*args, **kwargs)
