"""Background monitor for agent lifecycle.

Detects when agent tmux sessions die without firing SESSION_END hooks
and marks their DB records accordingly. This prevents ghost agents from
appearing in the UI as 'active' indefinitely.

Runs as a periodic background task alongside the session lifecycle manager.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from gobby.agents.registry import RunningAgentRegistry
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.storage.agents import LocalAgentRunManager

if TYPE_CHECKING:
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.clones import LocalCloneManager

logger = logging.getLogger(__name__)


class AgentLifecycleMonitor:
    """Periodically checks if agent tmux sessions are still alive.

    When a tmux session dies without firing SESSION_END hooks, this
    monitor detects the orphan and:
    - Marks the agent_runs DB record as 'error'
    - Removes the agent from the in-memory registry
    - Releases any associated worktrees
    """

    def __init__(
        self,
        agent_registry: RunningAgentRegistry,
        agent_run_manager: LocalAgentRunManager,
        session_coordinator: SessionCoordinator | None = None,
        clone_storage: LocalCloneManager | None = None,
        check_interval_seconds: float = 30.0,
    ) -> None:
        self._registry = agent_registry
        self._agent_run_manager = agent_run_manager
        self._session_coordinator = session_coordinator
        self._clone_storage = clone_storage
        self._check_interval = check_interval_seconds
        self._tmux = TmuxSessionManager()
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def set_session_coordinator(self, coordinator: SessionCoordinator) -> None:
        """Inject session coordinator after construction (avoids circular init ordering)."""
        self._session_coordinator = coordinator

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

        while self._running:
            try:
                await self.check_dead_agents()
                await self.check_expired_agents()
            except Exception as e:
                logger.error(f"Agent lifecycle check error: {e}")

            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break

    async def check_dead_agents(self) -> int:
        """Check for dead agents (tmux and autonomous) and clean up.

        Returns:
            Number of dead agents cleaned up.
        """
        agents = self._registry.list_all()

        # Check terminal-mode agents that have a tmux session name
        tmux_agents = [a for a in agents if a.mode == "terminal" and a.tmux_session_name]

        cleaned = 0
        for agent in tmux_agents:
            try:
                tmux_name = agent.tmux_session_name
                assert tmux_name is not None  # guaranteed by filter above
                alive = await self._tmux.has_session(tmux_name)
                if alive:
                    continue

                logger.info(
                    f"Detected dead tmux session '{agent.tmux_session_name}' "
                    f"for agent {agent.run_id}"
                )

                # Mark DB record as error if still in active status
                # These are sync DB calls — run off the event loop to avoid blocking
                db_run = await asyncio.to_thread(self._agent_run_manager.get, agent.run_id)
                if db_run and db_run.status in ("pending", "running"):
                    await asyncio.to_thread(
                        self._agent_run_manager.fail,
                        agent.run_id,
                        error="tmux session died unexpectedly",
                    )
                    logger.info(f"Marked agent run {agent.run_id} as failed (dead tmux session)")

                # Remove from in-memory registry
                self._registry.remove(agent.run_id, status="failed")

                # Release worktrees
                if self._session_coordinator:
                    try:
                        self._session_coordinator.release_session_worktrees(agent.session_id)
                    except Exception as e:
                        logger.warning(f"Failed to release worktrees for agent {agent.run_id}: {e}")

                # Release clones
                if self._clone_storage and agent.clone_id:
                    try:
                        await asyncio.to_thread(self._clone_storage.release, agent.clone_id)
                    except Exception as e:
                        logger.warning(f"Failed to release clone for agent {agent.run_id}: {e}")

                cleaned += 1

            except Exception as e:
                logger.warning(f"Error checking agent {agent.run_id}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} dead tmux agent(s)")

        # Check autonomous/in_process agents with asyncio.Tasks
        task_agents = [
            a for a in agents if a.task is not None and a.mode in ("autonomous", "in_process")
        ]
        for agent in task_agents:
            task: asyncio.Task[object] = agent.task  # type: ignore[assignment]
            if not task.done():
                continue

            try:
                exc = task.exception()
                if exc:
                    error_msg = f"Autonomous agent failed: {exc}"
                    logger.info(f"Detected failed autonomous task for agent {agent.run_id}: {exc}")
                else:
                    error_msg = None
                    logger.info(f"Detected completed autonomous task for agent {agent.run_id}")
            except asyncio.CancelledError:
                error_msg = "Autonomous agent was cancelled"
                logger.info(f"Detected cancelled autonomous task for agent {agent.run_id}")

            try:
                # Update DB record
                db_run = await asyncio.to_thread(self._agent_run_manager.get, agent.run_id)
                if db_run and db_run.status in ("pending", "running"):
                    if error_msg:
                        await asyncio.to_thread(
                            self._agent_run_manager.fail,
                            agent.run_id,
                            error=error_msg,
                        )
                    else:
                        await asyncio.to_thread(
                            self._agent_run_manager.complete,
                            agent.run_id,
                            result="Completed (detected by lifecycle monitor)",
                        )

                # Remove from in-memory registry
                status = "failed" if error_msg else "completed"
                self._registry.remove(agent.run_id, status=status)

                # Release worktrees
                if self._session_coordinator:
                    try:
                        self._session_coordinator.release_session_worktrees(agent.session_id)
                    except Exception as e:
                        logger.warning(f"Failed to release worktrees for agent {agent.run_id}: {e}")

                # Release clones
                if self._clone_storage and agent.clone_id:
                    try:
                        await asyncio.to_thread(self._clone_storage.release, agent.clone_id)
                    except Exception as e:
                        logger.warning(f"Failed to release clone for agent {agent.run_id}: {e}")

                cleaned += 1

            except Exception as e:
                logger.warning(f"Error cleaning up autonomous agent {agent.run_id}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} dead agent(s) total")

        return cleaned

    async def check_expired_agents(self) -> int:
        """Check for agents that have exceeded their timeout and kill them.

        Returns:
            Number of expired agents cleaned up.
        """
        import datetime

        agents = self._registry.list_all()
        now = datetime.datetime.now(datetime.UTC)
        cleaned = 0

        for agent in agents:
            if not agent.timeout_seconds:
                continue

            age = (now - agent.started_at).total_seconds()
            if age <= agent.timeout_seconds:
                continue

            try:
                logger.info(
                    f"Agent {agent.run_id} exceeded timeout ({age:.1f}s > {agent.timeout_seconds}s). Killing..."
                )

                # Kill process via registry
                await self._registry.kill(
                    agent.run_id,
                    signal_name="TERM",
                    timeout=5.0,
                    close_terminal=True,
                )

                # Mark DB record as timeout
                await asyncio.to_thread(
                    self._agent_run_manager.fail,
                    agent.run_id,
                    error=f"Agent exceeded {agent.timeout_seconds}s timeout",
                )

                # Remove from in-memory registry if kill() didn't
                if self._registry.get(agent.run_id):
                    self._registry.remove(agent.run_id, status="timeout")

                # Release worktrees
                if self._session_coordinator:
                    try:
                        self._session_coordinator.release_session_worktrees(agent.session_id)
                    except Exception as e:
                        logger.warning(
                            f"Failed to release worktrees for expired agent {agent.run_id}: {e}"
                        )

                # Release clones
                if self._clone_storage and agent.clone_id:
                    try:
                        await asyncio.to_thread(self._clone_storage.release, agent.clone_id)
                    except Exception as e:
                        logger.warning(
                            f"Failed to release clone for expired agent {agent.run_id}: {e}"
                        )

                cleaned += 1

            except Exception as e:
                logger.warning(f"Error checking expiration for agent {agent.run_id}: {e}")

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} expired agent(s)")

        return cleaned

    async def cleanup_orphaned_db_runs(self) -> int:
        """One-shot cleanup for DB records orphaned after daemon restart.

        Checks agent_runs still in 'running' status that have no
        corresponding entry in the in-memory registry (meaning the daemon
        restarted and lost track of them). Marks them as failed.

        Returns:
            Number of orphaned runs cleaned up.
        """
        # Sync DB calls — run off the event loop to avoid blocking
        running_runs = await asyncio.to_thread(self._agent_run_manager.list_running)
        if not running_runs:
            return 0

        cleaned = 0
        for run in running_runs:
            # If the run is tracked in the registry, it's being monitored normally
            if self._registry.get(run.id):
                continue

            # Not in registry - this is an orphan from before daemon restart
            await asyncio.to_thread(
                self._agent_run_manager.fail,
                run.id,
                error="Orphaned agent run (daemon restarted while agent was running)",
            )
            logger.info(f"Cleaned up orphaned agent run {run.id}")
            cleaned += 1

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} orphaned agent run(s) from previous daemon session")

        return cleaned
