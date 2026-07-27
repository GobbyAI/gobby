from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pydantic

from gobby.agents.capture import terminate_managed_tmux_async
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.prompt_detector import PromptDetector, PromptKind
from gobby.agents.stall_classifier import StallClassifier, StallStatus
from gobby.agents.watchdog import TranscriptWatchdogReader, WatchdogReaderRegistry
from gobby.agents.watchdog.completed_turn_recovery import (
    format_reprompt_message,
    recover_completed_turn,
)
from gobby.agents.watchdog.models import (
    CapacityRecoveryState,
    CompletedTurnRecoveryState,
    WatchdogTranscriptSnapshot,
)
from gobby.servers.routes.sessions.statusline_activity import last_session_activity
from gobby.sessions.transcript_paths import _find_transcript_on_disk
from gobby.storage.attention import run_attention_entry_id
from gobby.utils.datetime import parse_stored_datetime
from gobby.workflows.step_context import StepWorkflowContext, get_active_step_workflow_context

if TYPE_CHECKING:
    from gobby.agents.agent_cleanup import AgentCleanupHandler
    from gobby.agents.attention_metadata import AttentionMetadataStore
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun, LocalAgentRunManager, TerminalAction
    from gobby.storage.attention import AttentionKind, AttentionStateManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.session_models import Session
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)
WATCHDOG_ACTOR = "agent_idle_watchdog"
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
REASONING_WATCHDOG_CONTINUATION = (
    "Gobby watchdog interrupted a long idle reasoning turn with no workflow progress. "
    "Continue from the current task context, avoid redoing completed analysis, finish the "
    "required Gobby lifecycle MCP transition, then call end_agent_run."
)


class IdleCheckHandler:
    """Handles idle detection and reprompting for agents."""

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
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self.db = db
        self._get_session_manager = get_session_manager
        self._tmux = tmux
        self._idle_detector = idle_detector
        self._cleanup_handler = cleanup_handler
        self._tmux_config = tmux_config
        self._task_manager = task_manager
        self._run_db_callback = run_db
        self._attention_manager = attention_manager
        self._attention_metadata_store = attention_metadata_store
        self._prompt_detector = prompt_detector
        self._stall_classifier = stall_classifier
        self._watchdog_readers = watchdog_readers
        self._capacity_recovery: dict[str, CapacityRecoveryState] = {}
        self._completed_turn_recovery: dict[str, CompletedTurnRecoveryState] = {}
        self._transcript_path_cache: dict[tuple[str, str, str], str] = {}

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    async def sync_attention(self, run: AgentRun, pane_output: str) -> None:
        """Persist the attention episode represented by the latest pane output."""
        manager = self._attention_manager
        if manager is None:
            return

        prompt_detector = self._prompt_detector.for_provider(run.provider)
        stall_classifier = self._stall_classifier.for_provider(run.provider)
        reason: PromptKind | None = None
        kind: AttentionKind | None = None
        detected = prompt_detector.detect_prompt(pane_output)
        approval_dismissed = (
            detected is not None
            and detected.kind == "approval"
            and prompt_detector.was_approval_prompt_dismissed(run.id, pane_output)
        )
        trust_dismissed = (
            detected is not None
            and detected.kind == "trust"
            and prompt_detector.was_dismissed(run.id)
        )
        if (
            detected is not None
            and detected.kind == "approval"
            and (not self._tmux_config.auto_enter_approval_prompts or approval_dismissed)
        ):
            reason = "approval"
            kind = "actionable"
        elif detected is not None and detected.kind == "trust" and trust_dismissed:
            reason = "trust"
            kind = "actionable"
        elif detected is not None and detected.kind == "question":
            reason = "question"
            kind = "actionable"
        else:
            classification = stall_classifier.classify(
                run.id,
                pane_output=pane_output,
                error=run.error,
            )
            if classification.status is StallStatus.PROVIDER_STALL:
                reason = "stall"
                kind = "non_actionable"

        entry_id = run_attention_entry_id(run.id)
        if self._attention_metadata_store is not None:
            if reason == "stall":
                self._attention_metadata_store.set(entry_id, "retrying provider", 30_000)
            elif approval_dismissed or trust_dismissed:
                self._attention_metadata_store.set(entry_id, "needs attention", 60_000)

        if reason is None or kind is None:
            await self._clear_attention_if_current(entry_id)
            return

        prompt_payload = (
            detected
            if detected is not None and detected.kind == reason
            else prompt_detector.prompt_payload(pane_output, kind=reason)
        )
        await manager.transition_async(
            self._run_db,
            entry_id,
            state="blocked",
            run_id=run.id,
            session_id=run.child_session_id,
            reason=reason,
            kind=kind,
            fingerprint=prompt_payload.fingerprint,
            payload=prompt_payload.to_payload(),
        )

    async def clear_attention_after_injection(self, run: AgentRun) -> None:
        """Clear the exact attention episode resolved by successful injection."""
        await self._clear_attention_if_current(run_attention_entry_id(run.id))

    async def clear_attention(self, run: AgentRun) -> None:
        """Authoritatively clear attention when a run becomes terminal."""
        if self._attention_manager is not None:
            await self._attention_manager.transition_async(
                self._run_db,
                run_attention_entry_id(run.id),
                state=None,
            )
        self._stall_classifier.clear(run.id)

    async def _clear_attention_if_current(self, entry_id: str) -> None:
        manager = self._attention_manager
        if manager is None:
            return
        current = await self._run_db(manager.get, entry_id)
        if current is None or current.state is None:
            return
        await manager.transition_async(
            self._run_db,
            entry_id,
            state=None,
            expected_attention_id=current.attention_id,
            expected_fingerprint=current.fingerprint,
        )

    async def _clear_tmux_session_name(self, run: AgentRun) -> None:
        if run.tmux_session_name:
            try:
                await self._run_db(
                    self._agent_run_manager.clear_tmux_session_name,
                    run.id,
                    run.tmux_session_name,
                )
            except Exception:
                logger.warning(
                    "Failed clearing tmux session name for run %s",
                    run.id,
                    exc_info=True,
                )

    async def check_idle_agents(self) -> int:
        """Check for idle agents and reprompt or fail them."""
        if not self._tmux_config.idle_check_enabled:
            self._capacity_recovery.clear()
            self._completed_turn_recovery.clear()
            self._transcript_path_cache.clear()
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)
        active_run_ids = {run.id for run in runs}
        self._capacity_recovery = {
            run_id: state
            for run_id, state in self._capacity_recovery.items()
            if run_id in active_run_ids
        }
        self._completed_turn_recovery = {
            run_id: state
            for run_id, state in self._completed_turn_recovery.items()
            if run_id in active_run_ids
        }
        self._transcript_path_cache = {
            key: transcript_path
            for key, transcript_path in self._transcript_path_cache.items()
            if key[0] in active_run_ids
        }

        handled = 0
        for run in runs:
            try:
                handled += await self._handle_idle_check(run)
            except Exception as e:
                logger.warning(
                    "Error checking idle state for agent %s: %s",
                    run.id,
                    e,
                    exc_info=True,
                )

        return handled

    async def _resolve_transcript_path(
        self,
        session: Session,
        *,
        run_id: str,
    ) -> str | None:
        """Resolve a readable transcript path without mutating the session row."""
        source = session.source or ""
        external_id = session.external_id or ""
        cache_key = (run_id, source, external_id)

        stale_keys = [
            key for key in self._transcript_path_cache if key[0] == run_id and key != cache_key
        ]
        for key in stale_keys:
            del self._transcript_path_cache[key]

        stored_path = session.transcript_path
        if stored_path and stored_path != "missing_transcript" and os.path.isfile(stored_path):
            return stored_path

        cached_path = self._transcript_path_cache.get(cache_key)
        if cached_path and os.path.isfile(cached_path):
            return cached_path
        self._transcript_path_cache.pop(cache_key, None)

        if not source or not external_id:
            return None
        discovered_path = await asyncio.to_thread(
            _find_transcript_on_disk,
            source,
            external_id,
        )
        if not discovered_path or not os.path.isfile(discovered_path):
            return None

        self._transcript_path_cache[cache_key] = discovered_path
        return discovered_path

    async def check_attention_agents(self) -> int:
        """Scan active panes for attention without waiting for idle eligibility."""
        if self._attention_manager is None:
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
                await self.sync_attention(run, pane_output)
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
        runs = self._agent_run_manager.list_active()
        return [r for r in runs if r.tmux_session_name]

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

    async def _recover_failed_reprompt_clear(self, run: AgentRun, tmux_name: str) -> bool:
        if not await self._tmux.has_session(tmux_name):
            logger.warning(
                "Cannot recover failed idle prompt clear for agent %s: tmux gone",
                run.id,
            )
            return False
        if not await self._tmux.send_keys(tmux_name, "C-c", literal=False):
            logger.warning("Failed to interrupt queued prompt while recovering agent %s", run.id)
            return False
        if not await self._tmux.send_keys(tmux_name, "Enter", literal=False):
            logger.warning("Failed to submit interrupt while recovering agent %s", run.id)
            return False
        if not await self._tmux.has_session(tmux_name):
            logger.warning("Cannot reprompt agent %s after recovery: tmux gone", run.id)
            return False
        return True

    async def _handle_idle_check(self, run: AgentRun) -> int:
        """Handle idle check for a single agent."""
        latest_run = await self._run_db(self._agent_run_manager.get, run.id)
        idle_detector = self._idle_detector.for_provider((latest_run or run).provider)
        if latest_run is None or latest_run.status not in ("pending", "running"):
            await self.clear_attention(latest_run or run)
            idle_detector.reset_idle(run.id)
            self._completed_turn_recovery.pop(run.id, None)
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
        session_manager = self._get_session_manager()
        session: Any | None = None

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

        session_source = getattr(session, "source", None)
        reader = self._watchdog_readers.for_provider(session_source or run.provider)
        has_capacity_probe = reader is not None and reader.capacity_pane_message is not None
        if session_recent and not has_capacity_probe:
            idle_detector.reset_idle(run.id)
            return 0

        pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
        if pane_output is None:
            return 0
        await self.sync_attention(run, pane_output)
        capacity_candidate = self._pane_has_capacity_message(pane_output, reader)

        status = idle_detector.detect(pane_output)
        queued_message_prompt_visible = prompt_detector.detect_queued_message_prompt(pane_output)

        if status == "context_full":
            logger.info("Agent %s hit context window limit - failing", run.id)
            await self._fail_idle_agent(run, reason="context window exhausted")
            return 1

        # Active output is liveness even when the session row looks stale.
        if status == "active" and not capacity_candidate:
            if (
                session_recent
                or not session_stale
                or idle_detector.should_fail(run.id, self._tmux_config.max_reprompt_attempts)
            ):
                idle_detector.reset_idle(run.id)
                return 0

        if idle_detector.has_unsubmitted_input(pane_output):
            logger.info(
                "Agent %s has unsubmitted prompt input visible; skipping idle reprompt", run.id
            )
            idle_detector.reset_idle(run.id)
            return 0

        transcript_snapshot: WatchdogTranscriptSnapshot | None = None
        transcript_path: str | None = None
        if reader is not None and session is not None and (session_stale or capacity_candidate):
            transcript_path = await self._resolve_transcript_path(session, run_id=run.id)
        if reader is not None and transcript_path is not None:
            try:
                transcript_snapshot = await reader.read(transcript_path)
            except OSError:
                logger.warning(
                    "Failed to read %s transcript for idle recovery on run %s",
                    reader.provider_id,
                    run.id,
                )

        if (
            capacity_candidate
            and transcript_path is not None
            and transcript_snapshot is not None
            and transcript_snapshot.has_conclusive_capacity_error
        ):
            return await self._recover_capacity_error(
                run,
                tmux_name=tmux_name,
                session_id=session_id,
                transcript_path=transcript_path,
                snapshot=transcript_snapshot,
            )

        if session_recent:
            idle_detector.reset_idle(run.id)
            return 0

        if idle_detector.should_fail(run.id, self._tmux_config.max_reprompt_attempts):
            if queued_message_prompt_visible:
                logger.info(
                    "Agent %s has a queued-message prompt visible; suppressing idle failure",
                    run.id,
                )
                return 0
            logger.info(
                "Agent %s still idle after %s reprompts — failing",
                run.id,
                self._tmux_config.max_reprompt_attempts,
            )
            await self._log_transcript_snapshot(
                run,
                reason="failing after max idle reprompts",
            )
            await self._fail_idle_agent(run, reason="idle after max reprompt attempts")
            return 1

        completed_turn_recovery_due: bool | None = None
        if transcript_snapshot is not None:
            completed_turn_recovery_due = self._completed_turn_recovery_due(
                transcript_snapshot,
                idle_timeout_seconds=idle_timeout_seconds,
            )
        if completed_turn_recovery_due is False:
            return 0
        if (
            completed_turn_recovery_due is True
            and transcript_snapshot is not None
            and transcript_path is not None
        ):
            return await recover_completed_turn(
                self,
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
            if session_stale and await self._recover_reasoning_idle(
                run,
                tmux_name=tmux_name,
                session=session,
                session_id=session_id,
                reader=reader,
                snapshot=transcript_snapshot,
            ):
                return 1

            logger.info("Reprompting idle agent %s", run.id)
            await self._log_transcript_snapshot(
                run,
                reason="reprompting apparently idle agent",
                snapshot=transcript_snapshot,
            )
            return int(await self._send_idle_reprompt(run, tmux_name=tmux_name))

        return 0

    @staticmethod
    def _pane_has_capacity_message(
        pane_output: str,
        reader: TranscriptWatchdogReader | None,
    ) -> bool:
        capacity_message = reader.capacity_pane_message if reader is not None else None
        if capacity_message is None:
            return False
        visible = _ANSI_ESCAPE_RE.sub("", pane_output)
        visible = _TERMINAL_CONTROL_RE.sub("", visible)
        normalized = " ".join(visible.split())
        return capacity_message in normalized

    async def _recover_capacity_error(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        session_id: str | None,
        transcript_path: str,
        snapshot: WatchdogTranscriptSnapshot,
    ) -> int:
        error_event = snapshot.provider_error_event
        if error_event is None:
            return 0

        state = self._capacity_recovery.get(run.id)
        if state is None or state.transcript_path != transcript_path:
            state = CapacityRecoveryState(transcript_path=transcript_path)
            self._capacity_recovery[run.id] = state

        if state.last_error_line_num == error_event.line_num:
            return 0

        latest_model_output = snapshot.latest_model_output_line_num
        if (
            state.last_error_line_num is not None
            and latest_model_output is not None
            and latest_model_output > state.last_error_line_num
        ):
            state.successful_reprompts = 0

        max_attempts = self._tmux_config.max_reprompt_attempts
        if state.successful_reprompts >= max_attempts:
            state.last_error_line_num = error_event.line_num
            logger.info(
                "Agent %s remained at provider capacity after %s reprompts — failing",
                run.id,
                max_attempts,
            )
            await self._log_transcript_snapshot(
                run,
                reason="failing after max provider-capacity reprompts",
                snapshot=snapshot,
            )
            await self._fail_idle_agent(
                run,
                reason="provider capacity after max reprompt attempts",
            )
            return 1

        attempt = state.successful_reprompts + 1
        logger.info(
            "Reprompting agent %s after provider-capacity error (%s/%s)",
            run.id,
            attempt,
            max_attempts,
        )
        await self._log_transcript_snapshot(
            run,
            reason="recovering provider-capacity error",
            snapshot=snapshot,
        )
        if not await self._send_idle_reprompt(run, tmux_name=tmux_name):
            return 0

        state.last_error_line_num = error_event.line_num
        state.successful_reprompts = attempt
        await self._record_watchdog_task_event(
            run,
            action="capacity_reprompt",
            session_id=session_id,
            detail=(
                f"capacity_error={snapshot.provider_error_reason};attempt={attempt}/{max_attempts}"
            ),
        )
        return 1

    async def _send_idle_reprompt(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        reprompt_message: str | None = None,
    ) -> bool:
        if reprompt_message is None:
            reprompt_message = await self._idle_reprompt_message(run)
        cleared = await self._tmux.send_keys(tmux_name, "Escape", literal=False)
        if not cleared:
            logger.warning("Failed to clear queued prompt before reprompting agent %s", run.id)
            if not await self._recover_failed_reprompt_clear(run, tmux_name):
                return False
        sent = await self._tmux.send_keys(tmux_name, reprompt_message)
        if not sent:
            logger.warning("Failed to send idle reprompt text to agent %s", run.id)
            return False
        submitted = await self._tmux.send_keys(tmux_name, "Enter", literal=False)
        if not submitted:
            logger.warning("Failed to submit idle reprompt for agent %s", run.id)
            return False
        self._idle_detector.for_provider(run.provider).record_reprompt(run.id)
        return True

    async def _load_step_workflow_context(
        self,
        run: AgentRun,
    ) -> tuple[StepWorkflowContext | None, bool]:
        try:
            step_context = await self._run_db(
                get_active_step_workflow_context,
                self.db,
                run.child_session_id,
            )
        except psycopg.DatabaseError:
            logger.warning(
                "Database error loading active step workflow context for idle reprompt on "
                "run %s session %s",
                run.id,
                run.child_session_id,
                exc_info=True,
            )
            return None, False
        except (json.JSONDecodeError, pydantic.ValidationError, TypeError, ValueError):
            logger.warning(
                "Malformed active step workflow context for idle reprompt on run %s session %s",
                run.id,
                run.child_session_id,
                exc_info=True,
            )
            return None, False
        except Exception:
            logger.exception(
                "Unexpected error loading active step workflow context for idle reprompt "
                "on run %s session %s",
                run.id,
                run.child_session_id,
            )
            return None, False
        return step_context, True

    async def _idle_reprompt_message(
        self,
        run: AgentRun,
        *,
        step_context: StepWorkflowContext | None = None,
        context_resolved: bool = False,
    ) -> str:
        """Return an idle continuation prompt tuned to active step workflows."""
        if not context_resolved:
            step_context, _lookup_succeeded = await self._load_step_workflow_context(run)
        return format_reprompt_message(
            step_context,
            fallback_message=IdleDetector.REPROMPT_MESSAGE,
        )

    @staticmethod
    def _completed_turn_recovery_due(
        snapshot: WatchdogTranscriptSnapshot,
        *,
        idle_timeout_seconds: int,
    ) -> bool | None:
        if not snapshot.has_conclusive_turn_completed:
            return None

        event = snapshot.latest_turn_event
        if event is None or event.timestamp is None:
            return None
        elapsed = (datetime.now(UTC) - event.timestamp).total_seconds()
        return elapsed >= idle_timeout_seconds

    async def _recover_reasoning_idle(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        session: Any | None,
        session_id: str | None,
        reader: TranscriptWatchdogReader | None,
        snapshot: WatchdogTranscriptSnapshot | None = None,
    ) -> bool:
        """Interrupt a stale reasoning turn and send a focused continuation."""
        if not self._tmux_config.reasoning_watchdog_interrupt_enabled:
            return False
        if reader is None or not reader.supports_reasoning_interrupt:
            return False

        if snapshot is None:
            if session is None:
                return False
            transcript_path = await self._resolve_transcript_path(session, run_id=run.id)
            if transcript_path is None:
                return False
            try:
                snapshot = await reader.read(transcript_path)
            except OSError:
                logger.warning(
                    "Failed to read %s transcript for reasoning watchdog on run %s",
                    reader.provider_id,
                    run.id,
                )
                return False

        if snapshot.latest_activity_kind != "reasoning":
            return False

        logger.warning(
            "Reasoning watchdog interrupting %s run %s session %s: %s",
            reader.provider_id,
            run.id,
            session_id,
            json.dumps(snapshot.to_log_dict(), ensure_ascii=True),
        )

        interrupted = await self._tmux.send_keys(tmux_name, "C-c", literal=False)
        if not interrupted:
            return False

        settle_seconds = self._tmux_config.reasoning_watchdog_settle_seconds
        if settle_seconds:
            await asyncio.sleep(settle_seconds)

        sent = await self._tmux.send_keys(tmux_name, REASONING_WATCHDOG_CONTINUATION)
        if not sent:
            return False
        submitted = await self._tmux.send_keys(tmux_name, "Enter", literal=False)
        if not submitted:
            return False

        self._idle_detector.for_provider(run.provider).record_reprompt(run.id)
        await self._record_watchdog_task_event(
            run,
            action="reasoning_interrupt",
            session_id=session_id,
            detail="latest_activity_kind=reasoning",
        )
        return True

    async def _record_watchdog_task_event(
        self,
        run: AgentRun,
        *,
        action: str,
        session_id: str | None,
        detail: str,
    ) -> None:
        """Append a durable task-history event for watchdog recovery actions."""
        if self._task_manager is None or not run.task_id:
            return

        try:
            task = await self._run_db(self._task_manager.get_task, run.task_id)
        except Exception:
            logger.warning(
                "Failed to load task %s for idle watchdog audit on run %s",
                run.task_id,
                run.id,
                exc_info=True,
            )
            return
        if task is None:
            return

        try:
            from gobby.tasks.state_semantics import projected_task_state

            state = projected_task_state(task)
        except Exception:
            state = "agent_watchdog"

        reason = f"agent_idle_watchdog:{action} run_id={run.id}"
        if session_id:
            reason = f"{reason} session_id={session_id}"
        reason = f"{reason} {detail}"

        try:
            await self._run_db(
                self._task_manager.lifecycle_events.record_lifecycle_event,
                run.task_id,
                from_state=state,
                to_state=state,
                reason=reason,
                by_actor=WATCHDOG_ACTOR,
            )
        except Exception:
            logger.warning(
                "Failed to record idle watchdog audit for run %s task %s",
                run.id,
                run.task_id,
                exc_info=True,
            )

    async def _fail_idle_agent(self, run: AgentRun, reason: str) -> None:
        """Fail an agent that is irrecoverably idle."""
        if run.tmux_session_name:

            async def terminalize(
                _action: TerminalAction,
                payload: str | None,
            ) -> AgentRun | None:
                await self._cleanup_handler.cleanup_agent(
                    run,
                    terminal_payload=payload or f"Agent idle: {reason}",
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
                reason=f"Agent idle: {reason}",
                terminalize=terminalize,
            )
            if not result.success:
                logger.warning(
                    "Idle-agent termination failed for run %s: %s (%s)",
                    run.id,
                    result.error,
                    result.error_code,
                )
                return

            self._idle_detector.clear_state(run.id)
            self._completed_turn_recovery.pop(run.id, None)
            return

        self._idle_detector.clear_state(run.id)
        await self._cleanup_handler.cleanup_agent(run, terminal_payload=f"Agent idle: {reason}")
        self._completed_turn_recovery.pop(run.id, None)

    async def _log_transcript_snapshot(
        self,
        run: AgentRun,
        *,
        reason: str,
        snapshot: WatchdogTranscriptSnapshot | None = None,
        level: int = logging.WARNING,
    ) -> None:
        session_id = run.child_session_id
        if not session_id:
            return

        if snapshot is None:
            session_manager = self._get_session_manager()
            if session_manager is None:
                return

            try:
                session = await self._run_db(session_manager.get, session_id)
            except Exception:
                logger.warning(
                    "Failed to load session %s for watchdog idle diagnostics on run %s",
                    session_id,
                    run.id,
                )
                return

            if session is None:
                return

            provider_id = getattr(session, "source", None) or run.provider
            reader = self._watchdog_readers.for_provider(provider_id)
            if reader is None:
                return
            transcript_path = await self._resolve_transcript_path(session, run_id=run.id)
            if transcript_path is None:
                logger.warning(
                    "Watchdog idle diagnostic for %s run %s (%s): "
                    "session %s has no readable transcript path",
                    reader.provider_id,
                    run.id,
                    reason,
                    session_id,
                )
                return

            try:
                snapshot = await reader.read(transcript_path)
            except OSError:
                logger.warning(
                    "Failed to read %s transcript for idle diagnostic on run %s (%s)",
                    reader.provider_id,
                    run.id,
                    reason,
                )
                return

        if not snapshot.tail and snapshot.latest_turn_event is None:
            logger.warning(
                "Watchdog idle diagnostic for %s run %s (%s): "
                "no transcript summaries for session %s",
                snapshot.provider,
                run.id,
                reason,
                session_id,
            )
            return

        log_args = (
            snapshot.provider,
            run.id,
            reason,
            session_id,
            json.dumps(snapshot.to_log_dict(), ensure_ascii=True),
        )
        if level == logging.WARNING:
            logger.warning(
                "Watchdog idle diagnostic for %s run %s (%s) session %s: %s",
                *log_args,
            )
        else:
            logger.log(
                level,
                "Watchdog idle diagnostic for %s run %s (%s) session %s: %s",
                *log_args,
            )
