"""Background monitor for agent lifecycle.

Detects when agent processes die without firing SESSION_END hooks
and marks their DB records accordingly. Fully DB-driven — survives
daemon restarts without losing track of agents.

Runs as a periodic background task alongside the session lifecycle manager.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from gobby.agents.agent_cleanup import AgentCleanupHandler
from gobby.agents.agent_health import AgentHealthMonitor
from gobby.agents.capture import terminate_managed_tmux_async
from gobby.agents.checkpoint_manager import CheckpointManager
from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.kill import kill_agent as kill_agent  # Re-imported for tests
from gobby.agents.loop_tracker import LoopTracker
from gobby.agents.memory_watchdog import MemoryWatchdogHandler
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier
from gobby.agents.task_recovery import TaskRecoveryHandler
from gobby.agents.terminal_prompt_monitor import TerminalPromptMonitor
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.watchdog import WatchdogReaderRegistry
from gobby.config.tmux import TmuxConfig
from gobby.storage.tasks import TaskDispatchMutexManager
from gobby.telemetry.instruments import inc_counter, observe_histogram

if TYPE_CHECKING:
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.agents.detection.registry import DetectionManifestRegistry
    from gobby.autonomous.stuck_detector import StuckDetector
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.agents import (
        AgentRun,
        AgentRunTerminalReason,
        LocalAgentRunManager,
        TerminalAction,
    )
    from gobby.storage.attention import AttentionStateManager
    from gobby.storage.checkpoints import LocalCheckpointManager
    from gobby.storage.clones import LocalCloneManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.worktrees import LocalWorktreeManager

logger = logging.getLogger(__name__)

DISPATCH_MUTEX_REFRESH_TTL_SECONDS = 600
DISPATCH_MUTEX_REFRESH_BATCH_SIZE = 100


def _has_dispatch_stage_context(run: AgentRun) -> bool:
    """Return whether ``resume_metadata_json`` carries dispatcher stage state.

    Expected schema is a JSON object with string ``stage_name``/``stage_state``
    fields either at top level or under an ``initial_variables`` object.
    """
    try:
        metadata = run.resume_metadata_json
        if not isinstance(metadata, dict):
            return False
        if isinstance(metadata.get("stage_name"), str) and isinstance(
            metadata.get("stage_state"), str
        ):
            return True
        initial_variables = metadata.get("initial_variables")
        return isinstance(initial_variables, dict) and (
            isinstance(initial_variables.get("stage_name"), str)
            and isinstance(initial_variables.get("stage_state"), str)
        )
    except Exception:
        return False


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
        db: HubDatabase,
        detection_registry: DetectionManifestRegistry,
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
        stuck_detector: StuckDetector | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
        attention_manager: AttentionStateManager | None = None,
        attention_metadata_store: AttentionMetadataStore | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self._db = db
        self._detection_registry = detection_registry
        self._run_db_callback = run_db
        self._session_manager = session_manager
        self._session_coordinator = session_coordinator
        self._clone_storage = clone_storage
        self._check_interval = check_interval_seconds
        self._completion_registry = completion_registry
        self._task_manager = task_manager
        if tmux_config is None:
            from gobby.agents.tmux import get_configured_tmux_config

            tmux_config = get_configured_tmux_config()
        self._tmux_config = tmux_config
        self._tmux = TmuxSessionManager(config=self._tmux_config)
        self._idle_detector = IdleDetector(detection_registry)
        self._prompt_detector = PromptDetector(detection_registry)
        self._stall_classifier = StallClassifier(detection_registry)
        self._watchdog_readers = WatchdogReaderRegistry()
        self._loop_tracker = LoopTracker(threshold=3)
        # In-memory tracking for inherently non-persistable state
        self._master_fds: dict[str, int] = {}

        # Handlers
        self._task_recovery = TaskRecoveryHandler(
            task_manager=task_manager,
            agent_run_manager=agent_run_manager,
            stall_classifier=self._stall_classifier,
            terminal_agent_killer=lambda run: kill_agent(
                cast("AgentRun", run),
                db,
                master_fd=self._master_fds.get(run.id),
                signal_name="TERM",
                timeout=5.0,
                close_terminal=True,
            ),
            run_db=run_db,
        )
        self._terminal_prompt_monitor = TerminalPromptMonitor(
            get_active_terminal_runs=self._get_active_terminal_runs,
            get_tmux=lambda: self._tmux,
            prompt_detector=self._prompt_detector,
            loop_tracker=self._loop_tracker,
            get_tmux_config=lambda: self._tmux_config,
            handle_looping_agent=lambda run: self._checkpoint_and_kill_looping_agent(run),
            on_prompt_injected=lambda run: self._idle_check_handler.clear_attention_after_injection(
                run
            ),
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
            kill_tmux_session=lambda name: self._tmux.kill_session(name, missing_ok=True),
            run_db=run_db,
            attention_manager=attention_manager,
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
            checkpoint_agent_work=lambda run: self._checkpoint_agent_work(run),
        )
        self._memory_watchdog = MemoryWatchdogHandler(
            agent_run_manager=agent_run_manager,
            db=db,
            tmux=self._tmux,
            cleanup_handler=self._cleanup_handler,
            tmux_config=self._tmux_config,
            run_db=run_db,
        )
        self._idle_check_handler = IdleCheckHandler(
            agent_run_manager=agent_run_manager,
            db=db,
            get_session_manager=lambda: self._session_manager,
            tmux=self._tmux,
            idle_detector=self._idle_detector,
            cleanup_handler=self._cleanup_handler,
            tmux_config=self._tmux_config,
            task_manager=task_manager,
            run_db=run_db,
            attention_manager=attention_manager,
            attention_metadata_store=attention_metadata_store,
            prompt_detector=self._prompt_detector,
            stall_classifier=self._stall_classifier,
            watchdog_readers=self._watchdog_readers,
        )

        self._checkpoint_manager = (
            CheckpointManager(checkpoint_storage) if checkpoint_storage else None
        )
        self._worktree_storage = worktree_storage
        self._project_manager = project_manager
        self._stuck_detector = stuck_detector
        self._dispatch_refresh_cursor = 0
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    def set_session_coordinator(self, coordinator: SessionCoordinator) -> None:
        """Inject session coordinator after construction (avoids circular init ordering)."""
        self._session_coordinator = coordinator

    @property
    def prompt_detector(self) -> PromptDetector:
        """Return the prompt detector shared by lifecycle consumers."""
        return self._prompt_detector

    @property
    def idle_detector(self) -> IdleDetector:
        """Return the idle-detector provider cache."""
        return self._idle_detector

    @property
    def stall_classifier(self) -> StallClassifier:
        """Return the stall classifier shared by lifecycle consumers."""
        return self._stall_classifier

    @property
    def detection_registry(self) -> DetectionManifestRegistry:
        """Return the shared live manifest registry."""
        return self._detection_registry

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
        """Terminalize a successful active run.

        Args:
            run_id: Agent run id to complete.
            notify_result: Payload sent to completion subscribers.
            message: Human-readable completion notification.
            completion_result: Optional persisted result override.

        Returns:
            True when the run was completed by this call; False for an already
            terminal or missing run.

        Delegates persistence, subscriber notification, and child-session cleanup
        to AgentCleanupHandler.
        """
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
        try:
            await self.reconcile_pending_terminations()
        except Exception:
            logger.warning("Startup termination reconciliation failed", exc_info=True)
        self._task = asyncio.create_task(
            self._check_loop(),
            name="agent-lifecycle-monitor",
        )
        logger.info("AgentLifecycleMonitor started (interval=%ss)", self._check_interval)

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
                logger.debug("Lifecycle check iteration %s", iteration)
                await self.reconcile_pending_terminations()
                await self.check_trust_prompts()
                await self.check_loop_prompts()
                await self.check_approval_prompts()
                await self.check_queued_continuation_prompts()
                await self.check_periodic_enters()
                await self.check_attention_agents()
                await self.check_unhealthy_agents()
                await self.check_agent_memory()
                await self.expire_terminal_run_sessions()
                await self.check_initialization_timeout()
                await self.check_idle_agents()
                await self.check_provider_stalls()
                await self.check_autonomous_stuck_agents()
                await self.refresh_active_run_dispatch_mutexes()

                if iteration > 0 and iteration % 10 == 0:
                    try:
                        cleaned = await self._run_db(self._agent_run_manager.cleanup_stale_runs)
                        if cleaned:
                            logger.info("Cleaned up %s stale agent runs", cleaned)
                    except Exception as e:
                        logger.warning("Stale run cleanup failed: %s", e)

                iteration += 1
            except Exception as e:
                logger.error("Agent lifecycle check error: %s", e)

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
        await self.recover_tasks_from_terminal_agents()
        return await self._cleanup_handler.expire_terminal_run_sessions()

    async def recover_tasks_from_terminal_agents(self) -> int:
        """Recover task ownership for already-terminal non-success agent runs."""
        return await self._task_recovery.recover_tasks_from_terminal_agents()

    async def check_trust_prompts(self) -> int:
        """Check for folder trust prompts and auto-dismiss them."""
        return await self._terminal_prompt_monitor.check_trust_prompts()

    async def check_loop_prompts(self) -> int:
        """Check for loop detection prompts and auto-dismiss them."""
        return await self._terminal_prompt_monitor.check_loop_prompts()

    async def check_approval_prompts(self) -> int:
        """Check for approval prompts and send Enter when explicitly permitted."""
        return await self._terminal_prompt_monitor.check_approval_prompts()

    async def check_queued_continuation_prompts(self) -> int:
        """Observe queued Gobby continuation prompts without submitting input."""
        return await self._terminal_prompt_monitor.check_queued_continuation_prompts()

    async def check_periodic_enters(self) -> int:
        """Periodically send Enter to active autonomous terminal agents."""
        return await self._terminal_prompt_monitor.check_periodic_enters()

    async def check_unhealthy_agents(self) -> int:
        """Detect and clean up dead or expired agents."""
        return await self._health_monitor.check_unhealthy_agents()

    async def check_agent_memory(self) -> int:
        return await self._memory_watchdog.check_agent_memory()

    async def check_idle_agents(self) -> int:
        """Check for idle agents and reprompt or fail them."""
        return await self._idle_check_handler.check_idle_agents()

    async def check_attention_agents(self) -> int:
        """Check active terminal panes for prompts and sustained provider stalls."""
        return await self._idle_check_handler.check_attention_agents()

    async def check_initialization_timeout(self) -> int:
        """Detect agents that never initialized (provider hung on connect)."""
        return await self._health_monitor.check_initialization_timeout()

    async def check_provider_stalls(self) -> int:
        """Check tmux agents for provider-side stalls (rate limits, outages)."""
        return await self._health_monitor.check_provider_stalls()

    async def reconcile_pending_terminations(self) -> int:
        """Re-drive interrupted capture/kill/terminal sequences."""
        runs = await self._run_db(self._agent_run_manager.list_termination_candidates)
        reconciled = 0
        for run in runs:
            if not run.tmux_session_name:
                logger.warning(
                    "Cannot reconcile termination for run %s without a tmux session name",
                    run.id,
                )
                continue

            action_value = run.pending_terminal_action
            if action_value in {"complete", "fail", "timeout", "cancel"}:
                action = cast("TerminalAction", action_value)
            elif run.tool_calls_count == 0 and run.turns_used == 0:
                action = "fail"
            else:
                action = "complete"
            reason = run.pending_terminal_reason
            if action == "fail" and not reason:
                reason = "Agent completed with no activity (0 tool calls, 0 turns)"

            async def terminalize(
                terminal_action: TerminalAction,
                payload: str | None,
                *,
                candidate: AgentRun = run,
            ) -> AgentRun | None:
                if terminal_action == "complete":
                    await self._cleanup_handler.terminalize_successful_run(
                        candidate.id,
                        notify_result={"status": "completed"},
                        message=f"Agent {candidate.id} completed",
                    )
                elif terminal_action == "cancel":
                    await self._cleanup_handler.terminalize_cancelled_run(
                        candidate.id,
                        terminal_reason=cast(
                            "AgentRunTerminalReason",
                            payload or "user_cancelled",
                        ),
                    )
                else:
                    await self._cleanup_handler.cleanup_agent(
                        candidate,
                        terminal_payload=payload or "Agent termination requested",
                        is_timeout=terminal_action == "timeout",
                    )
                return cast(
                    "AgentRun | None",
                    await self._run_db(self._agent_run_manager.get, candidate.id),
                )

            result = await terminate_managed_tmux_async(
                storage=self._agent_run_manager,
                run=run,
                tmux=self._tmux,
                action=action,
                reason=reason,
                terminalize=terminalize,
            )
            if result.success:
                reconciled += 1
            else:
                logger.warning(
                    "Termination reconciliation failed for run %s: %s (%s)",
                    run.id,
                    result.error,
                    result.error_code,
                )
        return reconciled

    async def check_autonomous_stuck_agents(self) -> int:
        """Check active autonomous sessions with the production stuck detector."""
        if self._stuck_detector is None:
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)
        handled = 0
        for run in runs:
            session_id = run.child_session_id or run.claimed_session_id or run.parent_session_id
            if not session_id:
                continue
            try:
                result = await self._run_db(self._stuck_detector.is_stuck, session_id)
            except Exception as e:
                logger.warning("Autonomous stuck detection failed for %s: %s", session_id, e)
                continue
            if not result.is_stuck:
                continue

            handled += 1
            logger.warning(
                "Autonomous session stuck: session_id=%s run_id=%s layer=%s action=%s reason=%s",
                session_id,
                run.id,
                result.layer,
                result.suggested_action,
                result.reason,
            )
            if result.suggested_action in {"stop", "escalate"}:
                await self._cleanup_handler.cleanup_agent(
                    run,
                    terminal_payload=f"autonomous stuck: {result.reason or result.layer}",
                )
            elif run.tmux_session_name:
                await self._tmux.send_keys(run.tmux_session_name, "Enter", literal=True)

        if handled:
            inc_counter("agent_lifecycle_autonomous_stuck_detected_total", handled)
        return handled

    async def refresh_active_run_dispatch_mutexes(self) -> int:
        """Extend or restore dispatch mutex leases for active task-bound runs."""

        def _refresh(start_cursor: int) -> tuple[int, int, int]:
            storage = TaskDispatchMutexManager(self._db)
            refreshed = 0
            skipped = 0
            runs = self._agent_run_manager.list_active(
                limit=DISPATCH_MUTEX_REFRESH_BATCH_SIZE,
                offset=start_cursor,
            )
            if not runs and start_cursor:
                start_cursor = 0
                runs = self._agent_run_manager.list_active(
                    limit=DISPATCH_MUTEX_REFRESH_BATCH_SIZE,
                    offset=0,
                )
            for run in runs:
                if not run.task_id:
                    skipped += 1
                    continue
                if storage.refresh_mutex_for_run(
                    run.task_id,
                    run.id,
                    lease_holder="dispatcher",
                    ttl_seconds=DISPATCH_MUTEX_REFRESH_TTL_SECONDS,
                ):
                    refreshed += 1
                    continue
                if storage.get_mutex(run.task_id) is not None:
                    skipped += 1
                    continue
                if not _has_dispatch_stage_context(run):
                    skipped += 1
                    continue
                if storage.acquire_mutex(
                    run.task_id,
                    holder="dispatcher",
                    kind="heartbeat",
                    ttl_seconds=DISPATCH_MUTEX_REFRESH_TTL_SECONDS,
                    run_id=run.id,
                ):
                    refreshed += 1
                    continue
                skipped += 1
            next_cursor = (
                start_cursor + len(runs) if len(runs) == DISPATCH_MUTEX_REFRESH_BATCH_SIZE else 0
            )
            return refreshed, skipped, next_cursor

        try:
            start = time.perf_counter()
            refreshed, skipped, next_cursor = cast(
                tuple[int, int, int],
                await self._run_db(_refresh, self._dispatch_refresh_cursor),
            )
            self._dispatch_refresh_cursor = next_cursor
            if refreshed:
                inc_counter("agent_lifecycle_dispatch_mutex_refreshed_runs_total", refreshed)
            if skipped:
                inc_counter("agent_lifecycle_dispatch_mutex_skipped_runs_total", skipped)
            observe_histogram(
                "agent_lifecycle_dispatch_mutex_refresh_seconds",
                time.perf_counter() - start,
            )
            return refreshed
        except Exception as e:
            logger.warning("Failed to refresh active run dispatch mutexes: %s", e)
            return 0

    async def _checkpoint_and_kill_looping_agent(self, run: AgentRun) -> None:
        """Checkpoint work, kill tmux, then full cleanup for a doom-looping agent."""
        await self._checkpoint_agent_work(run)

        threshold = self._loop_tracker.threshold
        reason = f"doom loop: dismissed loop prompt {threshold}+ times"

        if run.tmux_session_name:

            async def terminalize(
                _action: TerminalAction,
                payload: str | None,
            ) -> AgentRun | None:
                await self._cleanup_handler.cleanup_agent(
                    run,
                    terminal_payload=payload or reason,
                )
                return cast(
                    "AgentRun | None",
                    await self._run_db(self._agent_run_manager.get, run.id),
                )

            result = await terminate_managed_tmux_async(
                storage=self._agent_run_manager,
                run=run,
                tmux=self._tmux,
                action="fail",
                reason=reason,
                terminalize=terminalize,
            )
            if not result.success:
                logger.warning(
                    "Doom-loop termination failed for run %s: %s (%s)",
                    run.id,
                    result.error,
                    result.error_code,
                )
            return

        await self._cleanup_handler.cleanup_agent(
            run,
            terminal_payload=reason,
        )

    async def _checkpoint_agent_work(self, run: AgentRun) -> None:
        """Checkpoint agent work when checkpoint storage is available."""
        if self._checkpoint_manager and run.task_id:
            cwd = await self._resolve_agent_cwd(run)
            if cwd:
                try:
                    checkpoint = await asyncio.to_thread(
                        self._checkpoint_manager.create_checkpoint,
                        cwd,
                        run.task_id,
                        run.child_session_id,
                        run.id,
                    )
                    if checkpoint:
                        logger.info(
                            "Checkpointed agent %s work: %s (%s files)",
                            run.id,
                            checkpoint.ref_name,
                            checkpoint.files_changed,
                        )
                except Exception as e:
                    logger.warning("Failed to checkpoint agent %s: %s", run.id, e)

    async def _resolve_agent_cwd(self, run: AgentRun) -> str | None:
        """Resolve the working directory for an agent run."""
        if run.worktree_id and self._worktree_storage:
            try:
                wt = await self._run_db(self._worktree_storage.get, run.worktree_id)
                if wt and wt.worktree_path:
                    return cast(str, wt.worktree_path)
            except Exception:
                logger.debug(
                    "Failed to resolve worktree %s for run %s",
                    run.worktree_id,
                    run.id,
                    exc_info=True,
                )

        if run.clone_id and self._clone_storage:
            try:
                clone = await self._run_db(self._clone_storage.get, run.clone_id)
                if clone and clone.clone_path:
                    return cast(str, clone.clone_path)
            except Exception:
                logger.debug(
                    "Failed to resolve clone %s for run %s", run.clone_id, run.id, exc_info=True
                )

        if run.child_session_id and self._session_manager:
            try:
                session = await self._run_db(self._session_manager.get, run.child_session_id)
                if session and session.project_id:
                    pm = self._project_manager
                    if pm is None:
                        from gobby.storage.projects import LocalProjectManager

                        pm = LocalProjectManager(self._db)
                        self._project_manager = pm
                    project = await self._run_db(pm.get, session.project_id)
                    if project and project.repo_path:
                        return str(project.repo_path)
            except Exception:
                logger.debug(
                    "Failed to resolve project path for session %s",
                    run.child_session_id,
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
