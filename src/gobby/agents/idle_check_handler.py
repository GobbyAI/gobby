from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.agents.attention_tracker import AgentAttentionTracker
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.watchdog.completed_turn_recovery import completed_turn_recovery_due
from gobby.agents.watchdog.models import WatchdogTranscriptSnapshot
from gobby.agents.watchdog.recovery import WatchdogRecoveryCoordinator
from gobby.agents.watchdog.transcript_resolver import WatchdogTranscriptResolver
from gobby.sessions.activity import last_session_activity
from gobby.sessions.machine_scope import is_local_machine_owner
from gobby.utils.datetime import parse_stored_datetime
from gobby.utils.machine_id import get_machine_id, require_machine_id

if TYPE_CHECKING:
    from gobby.agents.agent_cleanup import AgentCleanupHandler
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.agents.prompt_detector import PromptDetector
    from gobby.agents.stall_classifier import StallClassifier
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.agents.watchdog import WatchdogReaderRegistry
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun, LocalAgentRunManager
    from gobby.storage.attention import AttentionStateManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)


class IdleCheckHandler:
    """Orchestrate attention scans and per-run idle decisions."""

    def __init__(
        self,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        get_session_manager: Callable[[], SessionManager | None],
        tmux: TmuxSessionManager,
        idle_detector: IdleDetector,
        prompt_detector: PromptDetector,
        stall_classifier: StallClassifier,
        watchdog_readers: WatchdogReaderRegistry,
        cleanup_handler: AgentCleanupHandler,
        tmux_config: TmuxConfig,
        task_manager: LocalTaskManager | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
        attention_manager: AttentionStateManager | None = None,
        attention_metadata_store: AttentionMetadataStore | None = None,
        is_parked: Callable[[str], bool] | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self.db = db
        # session_id -> awaiting a subscribed completion (wait_for_agent parks the
        # turn; the daemon wakes the session with the result).
        self._is_parked = is_parked
        self._get_session_manager = get_session_manager
        self._tmux = tmux
        self._idle_detector = idle_detector
        self._prompt_detector = prompt_detector
        self._watchdog_readers = watchdog_readers
        self._tmux_config = tmux_config
        self._run_db_callback = run_db
        self._attention_tracker = AgentAttentionTracker(
            run_db=self._run_db,
            prompt_detector=prompt_detector,
            stall_classifier=stall_classifier,
            tmux_config=tmux_config,
            attention_manager=attention_manager,
            attention_metadata_store=attention_metadata_store,
        )
        self._transcript_resolver = WatchdogTranscriptResolver()
        self._recovery = WatchdogRecoveryCoordinator(
            agent_run_manager=agent_run_manager,
            db=db,
            get_session_manager=get_session_manager,
            tmux=tmux,
            idle_detector=idle_detector,
            watchdog_readers=watchdog_readers,
            cleanup_handler=cleanup_handler,
            tmux_config=tmux_config,
            transcript_resolver=self._transcript_resolver,
            run_db=self._run_db,
            task_manager=task_manager,
        )
        self._attention_panes_for_idle: dict[str, str] = {}

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    async def clear_attention_after_injection(self, run: AgentRun) -> None:
        await self._attention_tracker.clear_after_injection(run)

    async def check_idle_agents(self) -> int:
        """Check for idle agents and reprompt or fail them."""
        if not self._tmux_config.idle_check_enabled:
            self._recovery.clear()
            self._attention_panes_for_idle.clear()
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)
        attention_panes = self._attention_panes_for_idle
        self._attention_panes_for_idle = {}
        self._recovery.prune({run.id for run in runs})

        handled = 0
        for run in runs:
            try:
                pane_output = attention_panes.get(run.id)
                handled += await self._handle_idle_check(
                    run,
                    pane_output=pane_output,
                    attention_synced=run.id in attention_panes,
                )
            except Exception as e:
                logger.warning(
                    "Error checking idle state for agent %s: %s",
                    run.id,
                    e,
                    exc_info=True,
                )
        return handled

    async def check_attention_agents(self, *, reuse_for_idle: bool = False) -> int:
        """Scan active panes for attention without waiting for idle eligibility."""
        self._attention_panes_for_idle.clear()
        if not self._attention_tracker.enabled:
            return 0
        runs = await self._run_db(self._get_active_terminal_runs)
        checked = 0
        for run in runs:
            tmux_name = run.tmux_session_name
            if tmux_name is None:
                continue
            try:
                pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
                if pane_output is None:
                    continue
                await self._attention_tracker.sync(run, pane_output)
                if reuse_for_idle:
                    self._attention_panes_for_idle[run.id] = pane_output
                checked += 1
            except Exception:
                logger.warning(
                    "Failed to scan attention state for agent %s",
                    run.id,
                    exc_info=True,
                )
        return checked

    def _get_active_terminal_runs(self) -> list[AgentRun]:
        """Get active terminal agent runs with tmux sessions from DB."""
        runs = self._agent_run_manager.list_active_for_machine(require_machine_id())
        return [run for run in runs if run.tmux_session_name]

    def _idle_timeout_seconds_for_run(self, run: AgentRun) -> int:
        """Return the idle timeout window for a run."""
        requested_effort = (run.requested_reasoning_effort or "").strip().lower()
        if requested_effort == "xhigh":
            return self._tmux_config.idle_timeout_seconds * 5
        return self._tmux_config.idle_timeout_seconds

    def _idle_reprompt_delay_seconds_for_run(self, run: AgentRun) -> int:
        """Return the semantic idle reprompt delay for a run."""
        return max(
            self._tmux_config.idle_reprompt_delay_seconds,
            self._idle_timeout_seconds_for_run(run),
        )

    async def _handle_idle_check(
        self,
        run: AgentRun,
        *,
        pane_output: str | None = None,
        attention_synced: bool = False,
    ) -> int:
        """Handle idle check for a single agent."""
        latest_run = await self._run_db(self._agent_run_manager.get, run.id)
        idle_detector = self._idle_detector.for_provider((latest_run or run).provider)
        if latest_run is None or latest_run.status not in ("pending", "running"):
            await self._attention_tracker.clear(latest_run or run)
            idle_detector.reset_idle(run.id)
            self._recovery.discard(run.id)
            return 0

        run = latest_run
        prompt_detector = self._prompt_detector.for_provider(run.provider)
        tmux_name = run.tmux_session_name
        if tmux_name is None:
            logger.warning("Skipping idle check for run %s: missing tmux name", run.id)
            idle_detector.reset_idle(run.id)
            return 0
        idle_timeout_seconds = self._idle_timeout_seconds_for_run(run)

        session_stale = False
        session_recent = False
        session_id = run.child_session_id
        if session_id and self._is_parked is not None and self._is_parked(session_id):
            logger.debug("Agent %s is parked on a subscribed completion; not idle", run.id)
            idle_detector.reset_idle(run.id)
            return 0
        session_manager = self._get_session_manager()
        session: Session | None = None

        if session_id and session_manager:
            session = await self._run_db(session_manager.get, session_id)
            if session and session.updated_at:
                try:
                    last_update = parse_stored_datetime(session.updated_at)
                    if last_update is not None:
                        elapsed = (datetime.now(UTC) - last_update).total_seconds()
                        if elapsed < idle_timeout_seconds:
                            session_recent = True
                        else:
                            session_stale = True
                except (ValueError, TypeError):
                    pass

        if session_id:
            activity_at = last_session_activity(session_id)
            if activity_at is not None:
                elapsed = (datetime.now(UTC) - activity_at).total_seconds()
                if elapsed < idle_timeout_seconds:
                    session_recent = True

        session_source = session.source if session is not None else None
        reader = self._watchdog_readers.for_provider(session_source or run.provider)
        has_capacity_probe = reader is not None and reader.capacity_pane_message is not None
        if session_recent and not has_capacity_probe:
            idle_detector.reset_idle(run.id)
            return 0

        if pane_output is None:
            pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
        if pane_output is None:
            return 0
        if not attention_synced:
            await self._attention_tracker.sync(run, pane_output)
        capacity_candidate = self._recovery._pane_has_capacity_message(pane_output, reader)

        status = idle_detector.detect(pane_output)
        if status == "unknown":
            idle_detector.reset_idle(run.id)
            return 0

        if status == "context_full":
            logger.info("Agent %s hit context window limit - failing", run.id)
            await self._recovery._fail_idle_agent(run, reason="context window exhausted")
            return 1

        if idle_detector.has_turn_in_flight(pane_output):
            # The provider is mid-turn (thinking phases write nothing to the
            # transcript for minutes); reprompting would queue junk into a live
            # turn. Stagnation deferral bounds a frozen spinner separately.
            logger.debug("Agent %s shows a turn in flight; not idle", run.id)
            idle_detector.reset_idle(run.id)
            return 0

        if status == "active" and not capacity_candidate:
            if (
                session_recent
                or not session_stale
                or idle_detector.should_fail(run.id, self._tmux_config.max_reprompt_attempts)
            ):
                idle_detector.reset_idle(run.id)
                return 0

        transcript_snapshot: WatchdogTranscriptSnapshot | None = None
        transcript_path: str | None = None
        if (
            reader is not None
            and session is not None
            and is_local_machine_owner(session.machine_id, get_machine_id())
            and (session_stale or capacity_candidate)
        ):
            transcript_path = await self._transcript_resolver.resolve(session, run_id=run.id)
        if reader is not None and transcript_path is not None:
            try:
                transcript_snapshot = await reader.read(transcript_path)
            except OSError:
                logger.warning(
                    "Failed to read %s transcript for idle recovery on run %s",
                    reader.provider_id,
                    run.id,
                )

        if idle_detector.has_unsubmitted_input(pane_output):
            # Managed runs are autonomous: nobody returns to submit draft composer
            # text, so a turn completed past the idle window must still recover
            # (the reprompt path clears the draft with Escape before typing).
            if (
                not session_recent
                and transcript_path is not None
                and transcript_snapshot is not None
                and completed_turn_recovery_due(
                    transcript_snapshot,
                    idle_timeout_seconds=idle_timeout_seconds,
                )
                is True
            ):
                return await self._recovery.recover_completed_turn(
                    run,
                    tmux_name=tmux_name,
                    session_id=session_id,
                    transcript_path=transcript_path,
                    snapshot=transcript_snapshot,
                )
            logger.info(
                "Agent %s has unsubmitted prompt input visible; skipping idle reprompt",
                run.id,
            )
            idle_detector.reset_idle(run.id)
            return 0

        if (
            capacity_candidate
            and transcript_path is not None
            and transcript_snapshot is not None
            and transcript_snapshot.has_conclusive_capacity_error
        ):
            return await self._recovery._recover_capacity_error(
                run,
                tmux_name=tmux_name,
                session_id=session_id,
                transcript_path=transcript_path,
                snapshot=transcript_snapshot,
            )

        if session_recent:
            idle_detector.reset_idle(run.id)
            return 0

        queued_message_prompt_visible = prompt_detector.detect_queued_message_prompt(pane_output)
        if idle_detector.should_fail(run.id, self._tmux_config.max_reprompt_attempts):
            if queued_message_prompt_visible:
                logger.info(
                    "Agent %s has a queued-message prompt visible; suppressing idle failure",
                    run.id,
                )
                return 0
            if await self._recovery._complete_if_step_workflow_finished(run):
                await self._recovery._log_transcript_snapshot(
                    run,
                    reason="completing idle agent parked on a satisfied workflow exit condition",
                    level=logging.INFO,
                )
                return 1
            logger.info(
                "Agent %s still idle after %s reprompts — failing",
                run.id,
                self._tmux_config.max_reprompt_attempts,
            )
            await self._recovery._log_transcript_snapshot(
                run,
                reason="failing after max idle reprompts",
            )
            await self._recovery._fail_idle_agent(
                run,
                reason="idle after max reprompt attempts",
            )
            return 1

        recovery_due: bool | None = None
        if transcript_snapshot is not None:
            recovery_due = completed_turn_recovery_due(
                transcript_snapshot,
                idle_timeout_seconds=idle_timeout_seconds,
            )
        if recovery_due is False:
            return 0
        if recovery_due is True and transcript_snapshot is not None and transcript_path is not None:
            return await self._recovery.recover_completed_turn(
                run,
                tmux_name=tmux_name,
                session_id=session_id,
                transcript_path=transcript_path,
                snapshot=transcript_snapshot,
            )

        if idle_detector.should_reprompt(
            run.id,
            self._idle_reprompt_delay_seconds_for_run(run),
            self._tmux_config.max_reprompt_attempts,
        ):
            if session_stale and await self._recovery._recover_reasoning_idle(
                run,
                tmux_name=tmux_name,
                session=session,
                session_id=session_id,
                reader=reader,
                snapshot=transcript_snapshot,
            ):
                return 1

            logger.info("Reprompting idle agent %s", run.id)
            await self._recovery._log_transcript_snapshot(
                run,
                reason="reprompting apparently idle agent",
                snapshot=transcript_snapshot,
            )
            return int(
                await self._recovery._send_idle_reprompt(
                    run,
                    tmux_name=tmux_name,
                )
            )

        return 0
