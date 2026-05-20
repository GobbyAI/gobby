from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

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
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)
SESSION_STATS_LOOKUP_TIMEOUT_SECONDS = 2.0


def cleanup_merged_task_artifacts_after_agent_exit(
    db: DatabaseProtocol,
    task_id: str,
) -> list[Any]:
    """Retry merge artifact cleanup once the owning agent is no longer active."""
    from gobby.build.controls import cleanup_successful_merge_artifacts
    from gobby.storage.tasks import LocalTaskManager

    task_manager = LocalTaskManager(db)
    merge_stage = task_manager.stage_states.get(task_id, "merge")
    if merge_stage is None or merge_stage.state != "done":
        return []
    return cleanup_successful_merge_artifacts(db, task_id)


class AgentCleanupHandler:
    """Handles terminal state transitions and cleanup for agent runs."""

    def __init__(
        self,
        agent_run_manager: LocalAgentRunManager,
        db: DatabaseProtocol,
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
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
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
        self._run_db = run_db

    async def _run_sqlite(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db(func, *args, **kwargs)

    async def notify_terminal_completion(
        self,
        run_id: str,
        *,
        result: dict[str, Any],
        message: str,
    ) -> None:
        """Notify waiters about a terminal run transition."""
        if not self._completion_registry:
            return
        try:
            await self._completion_registry.notify(run_id, result=result, message=message)
        except Exception as e:
            logger.warning(f"Failed to notify completion for {run_id}: {e}")

    async def post_terminal_cleanup(
        self,
        run: AgentRun,
        *,
        cleanup_session_id: str | None = None,
        allow_parent_session_fallback: bool = True,
    ) -> None:
        """Release in-memory and isolation state for a terminal agent run."""
        from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state

        session_id = cleanup_session_id
        if session_id is None:
            session_id = run.child_session_id
        if session_id is None and allow_parent_session_fallback:
            session_id = run.parent_session_id
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
                await self._run_sqlite(self._clone_storage.release, run.clone_id)
            except Exception as e:
                logger.warning(f"Failed to release clone for agent {run.id}: {e}")

        if session_manager and session_id:
            try:
                await self._run_sqlite(session_manager.update_status, session_id, "expired")
                logger.debug(f"Expired session {session_id} for agent {run.id}")
            except Exception as e:
                logger.warning(f"Failed to expire session for agent {run.id}: {e}")

        cleanup = await self._run_sqlite(
            cleanup_agent_runtime_state,
            self._db,
            run_id=run.id,
            child_session_id=run.child_session_id,
        )
        if cleanup.dispatch_mutex_rows or cleanup.workflow_instance_rows:
            logger.info(
                "Cleaned runtime state for agent %s: dispatch_mutex=%s workflow_instances=%s",
                run.id,
                cleanup.dispatch_mutex_rows,
                cleanup.workflow_instance_rows,
            )
        if run.task_id:
            try:
                artifacts = await self._run_sqlite(
                    cleanup_merged_task_artifacts_after_agent_exit,
                    self._db,
                    run.task_id,
                )
                deleted_count = len([artifact for artifact in artifacts if artifact.deleted])
                deferred_count = len([artifact for artifact in artifacts if artifact.deferred])
                if deleted_count or deferred_count:
                    logger.info(
                        "Post-agent merge artifact cleanup for %s: deleted=%s deferred=%s",
                        run.id,
                        deleted_count,
                        deferred_count,
                    )
            except Exception:
                logger.warning(
                    "Post-agent merge artifact cleanup failed for run %s task %s",
                    run.id,
                    run.task_id,
                    exc_info=True,
                )

    async def _completion_stats_for_run(self, run: AgentRun) -> tuple[int, int]:
        tool_calls_count = run.tool_calls_count or 0
        turns_used = run.turns_used or 0
        if not run.child_session_id or (tool_calls_count and turns_used):
            return tool_calls_count, turns_used

        session_manager = self._get_session_manager()
        if session_manager is None:
            return tool_calls_count, turns_used

        try:
            session = await asyncio.wait_for(
                self._run_sqlite(session_manager.get, run.child_session_id),
                timeout=SESSION_STATS_LOOKUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.debug("Timed out reading session stats for agent %s", run.id)
            return tool_calls_count, turns_used
        except Exception:
            logger.debug("Failed to read session stats for agent %s", run.id, exc_info=True)
            return tool_calls_count, turns_used
        if session is None:
            return tool_calls_count, turns_used

        session_tool_calls = getattr(session, "tool_call_count", None)
        session_turns = getattr(session, "turn_count", None)
        if (
            isinstance(session_tool_calls, int)
            and not isinstance(session_tool_calls, bool)
            and tool_calls_count == 0
        ):
            tool_calls_count = session_tool_calls
        if (
            isinstance(session_turns, int)
            and not isinstance(session_turns, bool)
            and turns_used == 0
        ):
            turns_used = session_turns
        return tool_calls_count, turns_used

    async def terminalize_successful_run(
        self,
        run_id: str,
        *,
        notify_result: dict[str, Any],
        message: str,
        completion_result: str | None = None,
    ) -> bool:
        """Complete an active run, notify subscribers, and clean child-owned state.

        Args:
            run_id: Agent run id to mark complete.
            notify_result: Payload delivered to completion subscribers.
            message: Completion message delivered alongside the payload.
            completion_result: Optional final result to persist instead of the
                run's current result.

        Returns:
            True when the run transitioned to complete; False when the run was
            already terminal or missing after cleanup reconciliation.
        """
        current = await self._run_sqlite(self._agent_run_manager.get, run_id)
        if current is None:
            logger.debug("Successful terminalization no-op for missing run %s", run_id)
            return False

        tool_calls_count, turns_used = await self._completion_stats_for_run(current)
        db_run = await self._run_sqlite(
            self._agent_run_manager.complete,
            run_id,
            result=completion_result,
            tool_calls_count=tool_calls_count,
            turns_used=turns_used,
        )
        if db_run is None:
            latest = await self._run_sqlite(self._agent_run_manager.get, run_id)
            logger.debug(
                "Successful terminalization no-op for run %s; current status=%s",
                run_id,
                latest.status if latest else "missing",
            )
            if latest is not None:
                await self.post_terminal_cleanup(
                    latest,
                    cleanup_session_id=latest.child_session_id,
                    allow_parent_session_fallback=False,
                )
            return False

        await self.notify_terminal_completion(db_run.id, result=notify_result, message=message)
        await self.post_terminal_cleanup(
            db_run,
            cleanup_session_id=db_run.child_session_id,
            allow_parent_session_fallback=False,
        )
        return True

    async def terminalize_cancelled_run(
        self,
        run_id: str,
        *,
        terminal_reason: AgentRunTerminalReason,
    ) -> bool:
        """Mark an active run cancelled, recover ownership, and notify waiters."""
        db_run = await self._run_sqlite(
            self._agent_run_manager.cancel,
            run_id,
            terminal_reason=terminal_reason,
        )
        if db_run is None:
            current = await self._run_sqlite(self._agent_run_manager.get, run_id)
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
        expired = await self._run_sqlite(self._agent_run_manager.expire_sessions_for_terminal_runs)
        if expired:
            logger.info("Expired %s session(s) for terminal agent runs", expired)
        return cast(int, expired)

    async def cleanup_stale_pending_runs(self) -> int:
        """Clean up agent runs stuck in pending status after daemon restart."""
        return cast(
            int,
            await self._run_sqlite(self._agent_run_manager.cleanup_stale_pending_runs),
        )

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
                updated = await self._run_sqlite(
                    self._agent_run_manager.complete,
                    run.id,
                    result=terminal_payload,
                )
                if updated is not None:
                    terminal_run = updated
                    transitioned = True
            elif is_timeout:
                updated = await self._run_sqlite(
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
                updated = await self._run_sqlite(
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
            current = await self._run_sqlite(self._agent_run_manager.get, run.id)
            logger.debug(
                "Terminal cleanup no-op for run %s; current status=%s",
                run.id,
                current.status if current else "missing",
            )
            if current is not None:
                terminal_run = current

        await self.post_terminal_cleanup(terminal_run)
