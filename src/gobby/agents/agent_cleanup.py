from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.agents.loop_tracker import LoopTracker
    from gobby.agents.prompt_detector import PromptDetector
    from gobby.agents.stall_classifier import StallClassifier
    from gobby.agents.task_recovery import TaskRecoveryHandler
    from gobby.agents.terminal_prompt_monitor import TerminalPromptMonitor
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.agents import (
        AgentRun,
        AgentRunTerminalReason,
        LocalAgentRunManager,
    )
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


class AgentCleanupHandler:
    """Handles terminal state transitions and cleanup for agent runs."""

    def __init__(
        self,
        agent_run_manager: LocalAgentRunManager,
        get_session_manager: Callable[[], SessionManager | None],
        get_session_coordinator: Callable[[], SessionCoordinator | None],
        clone_storage: LocalCloneManager | None,
        completion_registry: CompletionEventRegistry | None,
        task_recovery: TaskRecoveryHandler,
        prompt_detector: PromptDetector,
        terminal_prompt_monitor: TerminalPromptMonitor,
        stall_classifier: StallClassifier,
        loop_tracker: LoopTracker,
        master_fds: dict[str, int],
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._get_session_manager = get_session_manager
        self._get_session_coordinator = get_session_coordinator
        self._clone_storage = clone_storage
        self._completion_registry = completion_registry
        self._task_recovery = task_recovery
        self._prompt_detector = prompt_detector
        self._terminal_prompt_monitor = terminal_prompt_monitor
        self._stall_classifier = stall_classifier
        self._loop_tracker = loop_tracker
        self._master_fds = master_fds

    async def notify_terminal_completion(
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

    async def post_terminal_cleanup(self, run: AgentRun) -> None:
        """Release in-memory and isolation state for a terminal agent run."""
        session_id = run.child_session_id or run.parent_session_id
        session_manager = self._get_session_manager()
        session_coordinator = self._get_session_coordinator()

        fd = self._master_fds.pop(run.id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

        self._prompt_detector.clear(run.id)
        self._terminal_prompt_monitor.clear(run.id)
        self._stall_classifier.clear(run.id)
        self._loop_tracker.clear(run.id)

        if session_coordinator and session_id:
            try:
                session_coordinator.release_session_worktrees(session_id)
            except Exception as e:
                logger.warning(f"Failed to release worktrees for agent {run.id}: {e}")

        if self._clone_storage and run.clone_id:
            try:
                await asyncio.to_thread(self._clone_storage.release, run.clone_id)
            except Exception as e:
                logger.warning(f"Failed to release clone for agent {run.id}: {e}")

        if session_manager and session_id:
            try:
                await asyncio.to_thread(session_manager.update_status, session_id, "expired")
                logger.debug(f"Expired session {session_id} for agent {run.id}")
            except Exception as e:
                logger.warning(f"Failed to expire session for agent {run.id}: {e}")

    async def terminalize_cancelled_run(
        self,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason,
    ) -> bool:
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

        await self._task_recovery.recover_task_from_terminal_agent(db_run, outcome="cancelled")
        await self.notify_terminal_completion(
            db_run.id,
            result={
                "status": "cancelled",
                "terminal_reason": terminal_reason,
                "run_id": db_run.id,
            },
            message=f"Agent {db_run.id} cancelled",
        )
        await self.post_terminal_cleanup(db_run)
        return True

    async def expire_terminal_run_sessions(self) -> int:
        """Expire sessions whose agent run is already in a terminal state."""
        expired = await asyncio.to_thread(self._agent_run_manager.expire_sessions_for_terminal_runs)
        if expired:
            logger.info("Expired %s session(s) for terminal agent runs", expired)
        return expired

    async def cleanup_stale_pending_runs(self) -> int:
        """Clean up agent runs stuck in pending status after daemon restart."""
        return await asyncio.to_thread(self._agent_run_manager.cleanup_stale_pending_runs)

    async def cleanup_agent(
        self,
        run: AgentRun,
        terminal_payload: str,
        is_success: bool = False,
        is_timeout: bool = False,
    ) -> None:
        """Full cleanup chain for an agent that needs cleanup.

        ``terminal_payload`` is stored as the success result or the failure/
        timeout error, depending on the terminal transition.
        """
        terminal_run = run
        transitioned = False

        if run.status in ("pending", "running"):
            if is_success:
                updated = await asyncio.to_thread(
                    self._agent_run_manager.complete,
                    run.id,
                    result=terminal_payload,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
            elif is_timeout:
                updated = await asyncio.to_thread(
                    self._agent_run_manager.timeout,
                    run.id,
                    error=terminal_payload,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
                    logger.info(
                        "Marked agent run %s as timed out: %s",
                        run.id,
                        terminal_payload,
                    )
            else:
                updated = await asyncio.to_thread(
                    self._agent_run_manager.fail,
                    run.id,
                    error=terminal_payload,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
                    logger.info(
                        "Marked agent run %s as failed: %s",
                        run.id,
                        terminal_payload,
                    )

        if transitioned:
            if not is_success:
                await self._task_recovery.recover_task_from_terminal_agent(
                    terminal_run, outcome="failed"
                )

            if is_success:
                result_data: dict[str, str] = {"status": "completed"}
            else:
                result_data = {"status": "error", "error": terminal_payload}

            await self.notify_terminal_completion(
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

        await self.post_terminal_cleanup(terminal_run)
