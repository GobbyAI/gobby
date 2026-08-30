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
from gobby.agents.checkpoint_manager import CheckpointManager
from gobby.agents.idle_check_handler import IdleCheckHandler
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.kill import kill_agent as kill_agent  # Re-imported for tests
from gobby.agents.lifecycle_reconciliation import LifecycleReconciliation
from gobby.agents.loop_tracker import LoopTracker
from gobby.agents.memory_watchdog import MemoryWatchdogHandler
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.stall_classifier import StallClassifier
from gobby.agents.task_recovery import TaskRecoveryHandler
from gobby.agents.terminal_prompt_monitor import TerminalPromptMonitor
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.watchdog import WatchdogReaderRegistry
from gobby.config.tmux import TmuxConfig
from gobby.tasks.state_semantics import is_task_closed
from gobby.telemetry.instruments import inc_counter
from gobby.utils.machine_id import require_machine_id

if TYPE_CHECKING:
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.agents.detection.registry import DetectionManifestRegistry
    from gobby.autonomous.stuck_detector import StuckDetectionResult, StuckDetector
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
        terminal_services: Any | None = None,
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
        if terminal_services is None:
            from gobby.storage.terminals import TerminalManager
            from gobby.terminals import TerminalRuntimeRegistry
            from gobby.terminals.services import TerminalServices
            from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
            from gobby.terminals.write_coordinator import WriteCoordinator

            manager = TerminalManager(db)
            runtime = TmuxTerminalRuntime(self._tmux)
            registry = TerminalRuntimeRegistry()
            registry.register(runtime)
            terminal_services = TerminalServices(
                manager=manager,
                registry=registry,
                coordinator=WriteCoordinator(manager, registry),
            )
        self._terminal_services = terminal_services
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
                terminal_services=self._terminal_services,
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
            terminal_services=self._terminal_services,
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
            attention_manager=attention_manager,
            terminal_services=self._terminal_services,
        )
        self._reconciliation = LifecycleReconciliation(
            agent_run_manager=agent_run_manager,
            db=db,
            tmux=self._tmux,
            cleanup_handler=self._cleanup_handler,
            run_db=self._run_db,
            terminal_manager=self._terminal_services.manager,
            runtime_registry=self._terminal_services.registry,
            spawn_in_doubt_seconds=150.0,
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
            terminal_services=self._terminal_services,
        )
        self._memory_watchdog = MemoryWatchdogHandler(
            agent_run_manager=agent_run_manager,
            db=db,
            tmux=self._tmux,
            cleanup_handler=self._cleanup_handler,
            tmux_config=self._tmux_config,
            run_db=run_db,
            terminal_services=self._terminal_services,
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
            is_parked=(
                completion_registry.is_awaiting if completion_registry is not None else None
            ),
            terminal_services=self._terminal_services,
        )

        self._checkpoint_manager = (
            CheckpointManager(checkpoint_storage) if checkpoint_storage else None
        )
        self._worktree_storage = worktree_storage
        self._project_manager = project_manager
        self._stuck_detector = stuck_detector
        self._stuck_interventions: dict[str, tuple[str | None, str | None, str]] = {}
        self._draft_grace_observations: dict[str, tuple[str, float]] = {}
        self._reconciliation_callback: Callable[[], Awaitable[int]] | None = None
        self._non_task_resume_callback: Callable[[], Awaitable[int]] | None = None
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    def set_session_coordinator(self, coordinator: SessionCoordinator) -> None:
        """Inject session coordinator after construction (avoids circular init ordering)."""
        self._session_coordinator = coordinator

    def set_reconciliation_callback(
        self,
        callback: Callable[[], Awaitable[int]],
    ) -> None:
        """Set the serialized owner for recovery-pending reclassification."""
        self._reconciliation_callback = callback

    def set_non_task_resume_callback(
        self,
        callback: Callable[[], Awaitable[int]],
    ) -> None:
        """Set the retry owner for parked daemon-stop runs with no task."""
        self._non_task_resume_callback = callback

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
        terminal_reason: AgentRunTerminalReason | None = None,
    ) -> bool:
        """Terminalize a successful active run.

        Args:
            run_id: Agent run id to complete.
            notify_result: Payload sent to completion subscribers.
            message: Human-readable completion notification.
            completion_result: Optional persisted result override.
            terminal_reason: Optional reason for successful terminalization.

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
            terminal_reason=terminal_reason,
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
                if self._reconciliation_callback is not None:
                    try:
                        await self._reconciliation_callback()
                    except Exception as e:
                        logger.warning("Agent reconciliation callback failed: %s", e)
                if self._non_task_resume_callback is not None:
                    try:
                        await self._non_task_resume_callback()
                    except Exception as e:
                        logger.warning("Non-task resume callback failed: %s", e)
                await self.reconcile_pending_terminations()
                await self.reap_stale_pending()
                await self.check_trust_prompts()
                await self.check_loop_prompts()
                await self.check_approval_prompts()
                await self.check_queued_continuation_prompts()
                await self.check_periodic_enters()
                await self.check_attention_agents(reuse_for_idle=True)
                await self.check_unhealthy_agents()
                await self.check_agent_memory()
                await self.reap_daemon_stop_orphans()
                await self.expire_terminal_run_sessions()
                await self.check_initialization_timeout()
                await self.check_idle_agents()
                await self.check_provider_stalls()
                await self.check_completed_task_agents()
                await self.check_autonomous_stuck_agents()
                await self.refresh_active_run_dispatch_mutexes()

                if iteration > 0 and iteration % 10 == 0:
                    try:
                        cleaned = await self.run_acknowledged_stale_sweeps(
                            running_timeout_minutes=30,
                        )
                        if cleaned:
                            logger.info("Cleaned up %s stale agent runs", len(cleaned))
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
        """Get active terminal agent runs with tmux sessions from DB.

        Recovery-protected runs (provisional successors, reconciliation
        pending) are excluded so prompt monitors cannot kill them before
        their durable state resolves.
        """
        from gobby.agents.recovery_state import is_recovery_protected

        runs = self._agent_run_manager.list_active_for_machine(require_machine_id())
        return [r for r in runs if r.terminal_id and not is_recovery_protected(r)]

    async def expire_terminal_run_sessions(self) -> int:
        """Expire sessions whose agent run is already in a terminal state."""
        await self.recover_tasks_from_terminal_agents()
        return await self._cleanup_handler.expire_terminal_run_sessions()

    async def reap_daemon_stop_orphans(self) -> int:
        """Release durable parked ownership after the recovery window elapses."""
        runs = await self._run_db(
            self._agent_run_manager.list_daemon_stop_orphans,
            machine_id=require_machine_id(),
        )
        reaped = 0
        for run in runs:
            if not run.child_session_id:
                continue
            try:
                if await self._reap_daemon_stop_orphan(run):
                    reaped += 1
            except Exception:
                # One bad orphan must not abort the tick's sweep; the claim
                # marker stays set and the next tick retries the remainder.
                logger.warning(
                    "Failed to reap daemon-stop orphan %s",
                    run.id,
                    exc_info=True,
                )
        return reaped

    async def _reap_daemon_stop_orphan(self, run: AgentRun) -> bool:
        """Give up on one elapsed parked original, delivering to durable waiters."""
        from gobby.storage.agent_resume import (
            claim_daemon_stop_orphan_reap,
            expire_parked_daemon_session,
        )
        from gobby.storage.pipeline_subscribers import CompletionSubscriberManager
        from gobby.utils.datetime import utc_now

        claimed = await self._run_db(
            claim_daemon_stop_orphan_reap,
            self._db,
            original_run_id=run.id,
            child_session_id=run.child_session_id,
        )
        if not claimed:
            return False
        # Parked originals were never registered in the in-memory completion
        # registry, so seed it from the durable subscriber rows; the terminal
        # delivery below would otherwise wake nobody.
        if self._completion_registry is not None and not self._completion_registry.is_registered(
            run.id
        ):
            subscribers = await self._run_db(
                CompletionSubscriberManager(self._db).get_completion_subscribers,
                run.id,
            )
            if subscribers:
                self._completion_registry.register(
                    run.id,
                    subscribers=subscribers,
                    continuation_prompt=getattr(run, "continuation_prompt", None),
                )
        await self._task_recovery.recover_task_from_terminal_agent(
            run,
            outcome="cancelled",
        )
        await self._cleanup_handler.post_terminal_cleanup(
            run,
            cleanup_session_id=run.child_session_id,
            notification_result={
                "status": "cancelled",
                "terminal_reason": "daemon_stop",
                "run_id": run.id,
            },
            notification_message=f"Agent {run.id} recovery window expired",
            force_full_cleanup=True,
        )
        expire_kwargs: dict[str, object] = {
            "original_run_id": run.id,
            "child_session_id": run.child_session_id,
        }
        if self._session_manager is not None:
            expire_kwargs["status_notifier"] = self._session_manager._notify_status_transition
        await self._run_db(
            expire_parked_daemon_session,
            self._db,
            **expire_kwargs,
        )
        await self._run_db(
            self._agent_run_manager.merge_resume_metadata,
            run.id,
            {"daemon_stop_orphan_reaped_at": utc_now().isoformat()},
        )
        return True

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

    async def check_attention_agents(self, *, reuse_for_idle: bool = False) -> int:
        """Check active terminal panes for prompts and sustained provider stalls."""
        return await self._idle_check_handler.check_attention_agents(
            reuse_for_idle=reuse_for_idle,
        )

    async def check_initialization_timeout(self) -> int:
        """Detect agents that never initialized (provider hung on connect)."""
        return await self._health_monitor.check_initialization_timeout()

    async def check_provider_stalls(self) -> int:
        """Check tmux agents for provider-side stalls (rate limits, outages)."""
        return await self._health_monitor.check_provider_stalls()

    async def reconcile_pending_terminations(self) -> int:
        """Re-drive interrupted capture/kill/terminal sequences."""
        return await self._reconciliation.reconcile_pending_terminations(
            machine_id=require_machine_id()
        )

    async def reap_stale_pending(self) -> int:
        """Fail pending terminals rows whose spawn never resolved."""
        return await self._reconciliation.reap_stale_pending()

    async def check_autonomous_stuck_agents(self) -> int:
        """Check active autonomous sessions with the production stuck detector."""
        if self._stuck_detector is None:
            self._stuck_interventions.clear()
            self._draft_grace_observations.clear()
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)
        active_run_ids = {run.id for run in runs}
        for run_id in self._stuck_interventions.keys() - active_run_ids:
            self._stuck_interventions.pop(run_id, None)
        for run_id in self._draft_grace_observations.keys() - active_run_ids:
            self._draft_grace_observations.pop(run_id, None)

        handled = 0
        for run in runs:
            session_id = run.child_session_id or run.claimed_session_id or run.parent_session_id
            if not session_id:
                self._stuck_interventions.pop(run.id, None)
                self._draft_grace_observations.pop(run.id, None)
                continue
            try:
                result = await self._run_db(self._stuck_detector.is_stuck, session_id)
            except Exception as e:
                logger.warning("Autonomous stuck detection failed for %s: %s", session_id, e)
                continue
            if not result.is_stuck or self._parked_on_completion(session_id, result):
                self._stuck_interventions.pop(run.id, None)
                self._draft_grace_observations.pop(run.id, None)
                continue

            fingerprint = self._stuck_intervention_fingerprint(result)
            if self._stuck_interventions.get(run.id) == fingerprint:
                continue
            if await self._defer_stagnation_for_live_pane(run, result):
                continue
            self._stuck_interventions[run.id] = fingerprint

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
                if self._idle_after_delivered_result(run, result):
                    await self._cleanup_handler.terminalize_successful_run(
                        run.id,
                        notify_result={"status": "completed"},
                        message=f"Agent {run.id} completed (idle after delivering its result)",
                    )
                else:
                    await self._cleanup_handler.cleanup_agent(
                        run,
                        terminal_payload=f"autonomous stuck: {result.reason or result.layer}",
                    )
                self._draft_grace_observations.pop(run.id, None)
            elif run.terminal_id and self._terminal_services is not None:
                await self._terminal_services.write(
                    run,
                    action_key=f"stuck-enter:{run.id}",
                    kind="key",
                    payload="enter",
                )

        if handled:
            inc_counter("agent_lifecycle_autonomous_stuck_detected_total", handled)
        return handled

    def _parked_on_completion(self, session_id: str, result: StuckDetectionResult) -> bool:
        """Quiet by design: the session awaits a subscribed completion (wait_for_agent)."""
        return (
            result.layer == "progress_stagnation"
            and self._completion_registry is not None
            and self._completion_registry.is_awaiting(session_id)
        )

    @staticmethod
    def _idle_after_delivered_result(run: AgentRun, result: StuckDetectionResult) -> bool:
        """Quiet because finished: a taskless run already handed its result to the parent.

        Interactive CLIs stay at their prompt after the final message, so progress
        stagnation is the normal end of such a run rather than a failure. Task-bound
        runs keep the failure path so task recovery can release their claim.
        """
        return (
            result.layer == "progress_stagnation"
            and run.task_id is None
            and bool((run.result or "").strip())
        )

    async def _defer_stagnation_for_live_pane(
        self,
        run: AgentRun,
        result: StuckDetectionResult,
    ) -> bool:
        """Defer fatal progress stagnation while the pane keeps changing.

        Two live shapes qualify: draft input being typed at the prompt, and the
        provider's in-flight turn spinner, whose elapsed-time counter advances
        through thinking phases that emit no progress events. A frozen pane keeps
        one fingerprint, so the grace window still expires on a hung CLI.
        """
        if result.layer != "progress_stagnation" or result.suggested_action not in {
            "stop",
            "escalate",
        }:
            self._draft_grace_observations.pop(run.id, None)
            return False

        services = self._terminal_services
        if services is None or services.terminal_for(run) is None:
            self._draft_grace_observations.pop(run.id, None)
            return False
        try:
            snapshot = await services.snapshot(run, 15)
        except Exception:
            logger.warning(
                "Failed to inspect draft input for autonomous run %s",
                run.id,
                exc_info=True,
            )
            self._draft_grace_observations.pop(run.id, None)
            return False
        pane_output = None if snapshot is None else snapshot.text
        if pane_output is None:
            self._draft_grace_observations.pop(run.id, None)
            return False

        detector = self._idle_detector.for_provider(run.provider)
        draft_fingerprint = detector.turn_in_flight_fingerprint(
            pane_output
        ) or detector.unsubmitted_input_fingerprint(pane_output)
        if draft_fingerprint is None:
            self._draft_grace_observations.pop(run.id, None)
            return False

        now = time.monotonic()
        grace_seconds = self._idle_check_handler._idle_reprompt_delay_seconds_for_run(run)
        observation = self._draft_grace_observations.get(run.id)
        if observation is None or observation[0] != draft_fingerprint:
            self._draft_grace_observations[run.id] = (draft_fingerprint, now)
            logger.info(
                "Deferring autonomous progress stagnation for run %s by %s seconds: "
                "the pane shows live input or a turn in flight",
                run.id,
                grace_seconds,
            )
            return True
        return now - observation[1] < grace_seconds

    async def check_completed_task_agents(self) -> int:
        """Complete active task-bound runs whose authoritative task is closed."""
        task_manager = getattr(self, "_task_manager", None)
        if task_manager is None:
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)
        handled = 0
        for run in runs:
            if run.task_id is None:
                continue
            try:
                task = await self._run_db(task_manager.get_task, run.task_id)
            except Exception as e:
                logger.warning(
                    "Bound task lookup failed for active agent run %s task_id=%s: %s",
                    run.id,
                    run.task_id,
                    e,
                )
                continue
            if not is_task_closed(task):
                continue

            task_ref = f"#{task.seq_num}" if task.seq_num is not None else task.id[:8]
            completed = await self.terminalize_successful_run(
                run.id,
                notify_result={
                    "status": "success",
                    "run_id": run.id,
                    "task_id": run.task_id,
                },
                message=f"Agent {run.id} completed bound task {task_ref}",
                terminal_reason="task_completed",
            )
            if completed:
                handled += 1
                logger.info(
                    "Completed active agent run %s after bound task %s closed",
                    run.id,
                    task_ref,
                )

        return handled

    @staticmethod
    def _stuck_intervention_fingerprint(
        result: StuckDetectionResult,
    ) -> tuple[str | None, str | None, str]:
        details = result.details or {}
        tool_pattern = details.get("tool_pattern")
        discriminator = (
            f"tool:{tool_pattern}"
            if isinstance(tool_pattern, str) and tool_pattern
            else f"reason:{result.reason or ''}"
        )
        return result.layer, result.suggested_action, discriminator

    async def refresh_active_run_dispatch_mutexes(self) -> int:
        """Extend or restore dispatch mutex leases for active task-bound runs."""
        return await self._reconciliation.refresh_active_run_dispatch_mutexes(
            machine_id=require_machine_id()
        )

    async def _checkpoint_and_kill_looping_agent(self, run: AgentRun) -> None:
        """Checkpoint work, kill tmux, then full cleanup for a doom-looping agent."""
        await self._checkpoint_agent_work(run)

        threshold = self._loop_tracker.threshold
        reason = f"doom loop: dismissed loop prompt {threshold}+ times"

        terminal = self._terminal_services.terminal_for(run)
        if terminal is not None:

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

            from gobby.agents.capture import terminate_managed_runtime_async

            result = await terminate_managed_runtime_async(
                storage=self._agent_run_manager,
                run=run,
                terminal=terminal,
                runtime=self._terminal_services.runtime_for(terminal),
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
                    from gobby.agents.lifecycle_checkout import resolve_session_checkout_root

                    root = await self._run_db(
                        resolve_session_checkout_root, self._db, session
                    )
                    return str(root) if root else None
            except Exception:
                logger.debug(
                    "Failed to resolve project path for session %s",
                    run.child_session_id,
                    exc_info=True,
                )

        return None

    async def cleanup_stale_pending_runs(self) -> int:
        """Clean up agent runs stuck in pending status after daemon restart."""
        return await self._cleanup_handler.cleanup_stale_pending_runs(
            machine_id=require_machine_id()
        )

    async def run_acknowledged_stale_sweeps(
        self,
        *,
        running_timeout_minutes: int | None = None,
        pending_timeout_minutes: int | None = None,
    ) -> list[str]:
        """Run stale transitions through acknowledged completion delivery."""
        return await self._cleanup_handler.run_acknowledged_stale_sweeps(
            machine_id=require_machine_id(),
            running_timeout_minutes=running_timeout_minutes,
            pending_timeout_minutes=pending_timeout_minutes,
        )

    # Delegates for backward compatibility and test stability
    async def _cleanup_agent(self, *args: Any, **kwargs: Any) -> None:
        """Delegate to cleanup handler."""
        await self._cleanup_handler.cleanup_agent(*args, **kwargs)

    async def _recover_task_from_failed_agent(self, *args: Any, **kwargs: Any) -> None:
        """Delegate to task recovery handler."""
        await self._task_recovery.recover_task_from_failed_agent(*args, **kwargs)
