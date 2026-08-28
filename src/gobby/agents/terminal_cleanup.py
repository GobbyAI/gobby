from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.agents import terminal_delivery
from gobby.agents.srt_process_cleanup import reap_srt_runner_process_tree
from gobby.storage.attention import run_attention_entry_id

if TYPE_CHECKING:
    from gobby.agents.loop_tracker import LoopTracker
    from gobby.agents.prompt_detector import PromptDetector
    from gobby.agents.stall_classifier import StallClassifier
    from gobby.agents.terminal_prompt_monitor import TerminalPromptMonitor
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.agents import AgentRun, LocalAgentRunManager
    from gobby.storage.attention import AttentionStateManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def cleanup_merged_task_artifacts_after_agent_exit(
    db: HubDatabase,
    task_id: str,
    *,
    preserve_worktree_id: str | None = None,
) -> list[Any]:
    """Retry merge artifact cleanup once the owning agent is no longer active."""
    from gobby.build.controls import cleanup_successful_merge_artifacts
    from gobby.storage.tasks import LocalTaskManager

    task_manager = LocalTaskManager(db)

    def cleanup() -> list[Any]:
        if preserve_worktree_id:
            return cleanup_successful_merge_artifacts(
                db,
                task_id,
                preserve_worktree_ids={preserve_worktree_id},
            )
        return cleanup_successful_merge_artifacts(db, task_id)

    merge_stage = task_manager.stage_states.get(task_id, "merge")
    if merge_stage is not None and merge_stage.state == "done":
        return cleanup()

    task = task_manager.get_task(task_id)
    if task is None or task.closed_at is None or task.closed_reason != "already_implemented":
        return []
    return cleanup()


class TerminalResourceCleaner:
    """Release resources and runtime state owned by terminal agent runs."""

    def __init__(
        self,
        *,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        get_session_coordinator: Callable[[], SessionCoordinator | None],
        clone_storage: LocalCloneManager | None,
        completion_registry: CompletionEventRegistry | None,
        prompt_detector: PromptDetector,
        terminal_prompt_monitor: TerminalPromptMonitor,
        stall_classifier: StallClassifier,
        loop_tracker: LoopTracker,
        master_fds: dict[str, int],
        run_db: Callable[..., Awaitable[Any]],
        attention_manager: AttentionStateManager | None = None,
        terminal_services: Any | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
        self._get_session_coordinator = get_session_coordinator
        self._clone_storage = clone_storage
        self._completion_registry = completion_registry
        self._prompt_detector = prompt_detector
        self._terminal_prompt_monitor = terminal_prompt_monitor
        self._stall_classifier = stall_classifier
        self._loop_tracker = loop_tracker
        self._master_fds = master_fds
        self._run_db = run_db
        self._attention_manager = attention_manager
        self._terminal_services = terminal_services

    async def post_terminal_cleanup(
        self,
        run: AgentRun,
        *,
        cleanup_session_id: str | None = None,
        allow_parent_session_fallback: bool = False,
        notification_result: dict[str, Any] | None = None,
        notification_message: str = "",
        force_full_cleanup: bool = False,
    ) -> None:
        """Release in-memory and isolation state for a terminal agent run."""
        from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state

        parking = run.terminal_reason == "daemon_stop" and not force_full_cleanup
        session_id = cleanup_session_id
        if session_id is None:
            session_id = run.child_session_id
        if session_id is None and allow_parent_session_fallback:
            session_id = run.parent_session_id
        session_coordinator = self._get_session_coordinator()

        if not parking:
            await terminal_delivery.deliver_and_cleanup_terminal_run(
                db=self._db,
                completion_registry=self._completion_registry,
                run_id=run.id,
                result=notification_result,
                message=notification_message,
                run_db=self._run_db,
            )

        if self._attention_manager is not None:
            try:
                await self._attention_manager.transition_async(
                    self._run_db,
                    run_attention_entry_id(run.id),
                    state=None,
                )
            except Exception:
                logger.warning(
                    "Failed to clear attention for terminal agent %s",
                    run.id,
                    exc_info=True,
                )

        fd = self._master_fds.pop(run.id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        await self._close_tmux_session(run)
        if not parking:
            try:
                await reap_srt_runner_process_tree(run.id)
            except Exception:
                logger.warning(
                    "Failed to reap SRT sandbox runner for terminal agent %s",
                    run.id,
                    exc_info=True,
                )

        self._prompt_detector.clear(run.id)
        self._terminal_prompt_monitor.clear(run.id)
        self._stall_classifier.clear(run.id)
        self._loop_tracker.clear(run.id)

        if not parking and session_coordinator and session_id:
            try:
                session_coordinator.release_session_worktrees(session_id)
            except Exception as exc:
                logger.warning("Failed to release worktrees for agent %s: %s", run.id, exc)

        if not parking and self._clone_storage and run.clone_id:
            try:
                await self._run_db(self._clone_storage.release, run.clone_id)
            except Exception as exc:
                logger.warning("Failed to release clone for agent %s: %s", run.id, exc)

        cleanup = await self._run_db(
            cleanup_agent_runtime_state,
            self._db,
            run_id=run.id,
            child_session_id=run.child_session_id,
            terminal_reason=run.terminal_reason if parking else None,
        )
        if cleanup.dispatch_mutex_rows or cleanup.workflow_instance_rows:
            logger.debug(
                "Cleaned runtime state for agent %s: dispatch_mutex=%s agent_step_instances=%s",
                run.id,
                cleanup.dispatch_mutex_rows,
                cleanup.workflow_instance_rows,
            )
        if run.task_id and not parking:
            try:
                initial_variables = (run.resume_metadata_json or {}).get("initial_variables")
                reused_worktree = (
                    isinstance(initial_variables, dict)
                    and initial_variables.get("reused_worktree") is True
                )
                cleanup_kwargs = (
                    {"preserve_worktree_id": run.worktree_id}
                    if reused_worktree and run.worktree_id
                    else {}
                )
                artifacts = await self._run_db(
                    cleanup_merged_task_artifacts_after_agent_exit,
                    self._db,
                    run.task_id,
                    **cleanup_kwargs,
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

    async def _close_tmux_session(self, run: AgentRun) -> bool:
        from gobby.storage.terminals import TerminalManager

        if not run.terminal_id:
            return False
        latest = await self._run_db(self._agent_run_manager.get, run.id)
        if latest is None or latest.terminal_id != run.terminal_id:
            return False
        manager = TerminalManager(self._agent_run_manager.db)
        terminal = manager.get(run.terminal_id)
        if terminal is None or terminal.state not in {"pending", "live"}:
            return False
        if self._terminal_services is not None:
            try:
                runtime = self._terminal_services.runtime_for(terminal)
                await runtime.terminate(terminal, grace_seconds=5.0)
                if await runtime.is_live(terminal):
                    logger.warning(
                        "Terminal %s for agent %s was not closed",
                        run.terminal_id,
                        run.id,
                    )
                    return False
            except Exception:
                logger.warning(
                    "Failed to close lingering terminal %s for agent %s",
                    run.terminal_id,
                    run.id,
                    exc_info=True,
                )
                return False
        manager.mark_exited(run.terminal_id)
        # The stored pid is the pane pid of the terminal just closed; keeping it
        # would offer a stale signal target to later recovery.
        await self._run_db(self._agent_run_manager.update_runtime, run.id, pid=None)
        logger.info(
            "Closed lingering terminal %s for agent %s",
            run.terminal_id,
            run.id,
        )
        return True

    async def cleanup_terminal_tmux_sessions(self) -> int:
        """Close tmux sessions left behind for already-terminal agent runs."""
        runs = await self._run_db(self._agent_run_manager.list_terminal_with_tmux)
        closed = 0
        for run in runs:
            if await self._close_tmux_session(run):
                closed += 1
        return closed
