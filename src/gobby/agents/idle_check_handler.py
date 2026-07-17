from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import psycopg
import pydantic

from gobby.agents.capture import terminate_managed_tmux_async
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.prompt_detector import PromptDetector
from gobby.servers.routes.sessions.statusline_activity import last_session_activity
from gobby.utils.datetime import parse_stored_datetime
from gobby.workflows.step_context import get_active_step_workflow_context

if TYPE_CHECKING:
    from gobby.agents.agent_cleanup import AgentCleanupHandler
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun, LocalAgentRunManager, TerminalAction
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)
WATCHDOG_ACTOR = "agent_idle_watchdog"
_CODEX_MODEL_CAPACITY_MESSAGE = "Selected model is at capacity. Please try a different model."
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
REASONING_WATCHDOG_CONTINUATION = (
    "Gobby watchdog interrupted a long idle reasoning turn with no workflow progress. "
    "Continue from the current task context, avoid redoing completed analysis, finish the "
    "required Gobby lifecycle MCP transition, then call end_agent_run."
)


@dataclass(frozen=True, slots=True)
class _CodexTranscriptEventSummary:
    line_num: int
    timestamp: str | None
    event_type: str
    payload_type: str

    def to_log_dict(self) -> dict[str, object]:
        return {
            "line_num": self.line_num,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "payload_type": self.payload_type,
        }


@dataclass(frozen=True, slots=True)
class _CodexTranscriptSnapshot:
    response_items: tuple[_CodexTranscriptEventSummary, ...]
    lifecycle_event: _CodexTranscriptEventSummary | None
    task_started_event: _CodexTranscriptEventSummary | None
    capacity_error_event: _CodexTranscriptEventSummary | None
    latest_model_output_line_num: int | None
    last_malformed_line_num: int | None = None

    @property
    def latest_response_payload_type(self) -> str | None:
        if not self.response_items:
            return None
        return self.response_items[-1].payload_type

    @property
    def has_conclusive_task_complete(self) -> bool:
        event = self.lifecycle_event
        if event is None or event.payload_type != "task_complete":
            return False
        return self.last_malformed_line_num is None

    @property
    def has_conclusive_capacity_error(self) -> bool:
        started = self.task_started_event
        error = self.capacity_error_event
        completed = self.lifecycle_event
        if started is None or error is None or completed is None:
            return False
        if completed.payload_type != "task_complete":
            return False
        if not started.line_num < error.line_num < completed.line_num:
            return False
        return self.last_malformed_line_num is None

    def to_log_dict(self) -> dict[str, object]:
        return {
            "response_items": [item.to_log_dict() for item in self.response_items],
            "lifecycle_event": (
                self.lifecycle_event.to_log_dict() if self.lifecycle_event is not None else None
            ),
            "capacity_error_event": (
                self.capacity_error_event.to_log_dict()
                if self.capacity_error_event is not None
                else None
            ),
        }


@dataclass(slots=True)
class _CodexCapacityRecoveryState:
    transcript_path: str
    last_error_line_num: int | None = None
    successful_reprompts: int = 0


class IdleCheckHandler:
    """Handles idle detection and reprompting for agents."""

    def __init__(
        self,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        get_session_manager: Callable[[], SessionManager | None],
        tmux: TmuxSessionManager,
        idle_detector: IdleDetector,
        cleanup_handler: AgentCleanupHandler,
        tmux_config: TmuxConfig,
        task_manager: LocalTaskManager | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
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
        self._prompt_detector = PromptDetector()
        self._codex_capacity_recovery: dict[str, _CodexCapacityRecoveryState] = {}

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

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
            self._codex_capacity_recovery.clear()
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)
        active_run_ids = {run.id for run in runs}
        self._codex_capacity_recovery = {
            run_id: state
            for run_id, state in self._codex_capacity_recovery.items()
            if run_id in active_run_ids
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
        if latest_run is None or latest_run.status not in ("pending", "running"):
            self._idle_detector.reset_idle(run.id)
            return 0

        run = latest_run
        tmux_name = run.tmux_session_name
        if tmux_name is None:
            logger.warning("Skipping idle check for run %s: missing tmux name", run.id)
            self._idle_detector.reset_idle(run.id)
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

        is_codex = session is not None and getattr(session, "source", None) == "codex"
        if session_recent and not is_codex:
            self._idle_detector.reset_idle(run.id)
            return 0

        pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
        if pane_output is None:
            return 0
        capacity_candidate = is_codex and self._pane_has_codex_capacity_message(pane_output)

        queued_message_prompt_visible = False
        if pane_output is not None:
            status = self._idle_detector.detect(pane_output)
            queued_message_prompt_visible = self._prompt_detector.detect_queued_message_prompt(
                pane_output
            )

            if status == "context_full":
                logger.info("Agent %s hit context window limit - failing", run.id)
                await self._fail_idle_agent(run, reason="context window exhausted")
                return 1

            # Active output is liveness even when the session row looks stale.
            if status == "active" and not capacity_candidate:
                if (
                    session_recent
                    or not session_stale
                    or self._idle_detector.should_fail(
                        run.id, self._tmux_config.max_reprompt_attempts
                    )
                ):
                    self._idle_detector.reset_idle(run.id)
                    return 0

            if self._idle_detector.has_unsubmitted_input(pane_output):
                logger.info(
                    "Agent %s has unsubmitted prompt input visible; skipping idle reprompt",
                    run.id,
                )
                self._idle_detector.reset_idle(run.id)
                return 0

        transcript_snapshot: _CodexTranscriptSnapshot | None = None
        transcript_path = getattr(session, "transcript_path", None) if is_codex else None
        if (
            is_codex
            and isinstance(transcript_path, str)
            and transcript_path
            and (session_stale or capacity_candidate)
        ):
            try:
                transcript_snapshot = await self._read_codex_transcript_snapshot(transcript_path)
            except OSError:
                logger.warning(
                    "Failed to read Codex transcript for idle recovery on run %s",
                    run.id,
                )

        if (
            capacity_candidate
            and transcript_snapshot is not None
            and transcript_snapshot.has_conclusive_capacity_error
        ):
            return await self._recover_codex_capacity_error(
                run,
                tmux_name=tmux_name,
                session_id=session_id,
                transcript_path=cast(str, transcript_path),
                snapshot=transcript_snapshot,
            )

        if session_recent:
            self._idle_detector.reset_idle(run.id)
            return 0

        if self._idle_detector.should_fail(run.id, self._tmux_config.max_reprompt_attempts):
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
            await self._log_codex_transcript_snapshot(
                run,
                reason="failing after max idle reprompts",
            )
            await self._fail_idle_agent(run, reason="idle after max reprompt attempts")
            return 1

        completed_turn_recovery_due: bool | None = None
        if transcript_snapshot is not None:
            completed_turn_recovery_due = self._completed_codex_turn_recovery_due(
                transcript_snapshot,
                idle_timeout_seconds=idle_timeout_seconds,
            )
        if completed_turn_recovery_due is False:
            return 0
        if completed_turn_recovery_due is True:
            logger.info("Recovering completed Codex turn for idle agent %s", run.id)
            await self._log_codex_transcript_snapshot(
                run,
                reason="recovering completed Codex turn",
                snapshot=transcript_snapshot,
            )
            if not await self._send_idle_reprompt(run, tmux_name=tmux_name):
                return 0
            await self._record_watchdog_task_event(
                run,
                action="task_complete_reprompt",
                session_id=session_id,
                detail="latest_lifecycle_event=task_complete",
            )
            return 1

        if self._idle_detector.should_reprompt(
            run.id,
            self._idle_reprompt_delay_seconds_for_run(run),
            self._tmux_config.max_reprompt_attempts,
        ):
            if session_stale and await self._recover_reasoning_idle(
                run,
                tmux_name=tmux_name,
                session=session,
                session_id=session_id,
                snapshot=transcript_snapshot,
            ):
                return 1

            logger.info("Reprompting idle agent %s", run.id)
            await self._log_codex_transcript_snapshot(
                run,
                reason="reprompting apparently idle agent",
                snapshot=transcript_snapshot,
            )
            return int(await self._send_idle_reprompt(run, tmux_name=tmux_name))

        return 0

    @staticmethod
    def _pane_has_codex_capacity_message(pane_output: str) -> bool:
        visible = _ANSI_ESCAPE_RE.sub("", pane_output)
        visible = _TERMINAL_CONTROL_RE.sub("", visible)
        normalized = " ".join(visible.split())
        return _CODEX_MODEL_CAPACITY_MESSAGE in normalized

    async def _recover_codex_capacity_error(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        session_id: str | None,
        transcript_path: str,
        snapshot: _CodexTranscriptSnapshot,
    ) -> int:
        error_event = snapshot.capacity_error_event
        if error_event is None:
            return 0

        state = self._codex_capacity_recovery.get(run.id)
        if state is None or state.transcript_path != transcript_path:
            state = _CodexCapacityRecoveryState(transcript_path=transcript_path)
            self._codex_capacity_recovery[run.id] = state

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
                "Codex agent %s remained at capacity after %s reprompts — failing",
                run.id,
                max_attempts,
            )
            await self._log_codex_transcript_snapshot(
                run,
                reason="failing after max Codex capacity reprompts",
                snapshot=snapshot,
            )
            await self._fail_idle_agent(
                run,
                reason="Codex model capacity after max reprompt attempts",
            )
            return 1

        attempt = state.successful_reprompts + 1
        logger.info(
            "Reprompting Codex agent %s after model capacity error (%s/%s)",
            run.id,
            attempt,
            max_attempts,
        )
        await self._log_codex_transcript_snapshot(
            run,
            reason="recovering Codex model capacity error",
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
            detail=f"codex_error_info=server_overloaded;attempt={attempt}/{max_attempts}",
        )
        return 1

    async def _send_idle_reprompt(self, run: AgentRun, *, tmux_name: str) -> bool:
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
        self._idle_detector.record_reprompt(run.id)
        return True

    async def _idle_reprompt_message(self, run: AgentRun) -> str:
        """Return an idle continuation prompt tuned to active step workflows."""
        try:
            step_context = await self._run_db(
                get_active_step_workflow_context,
                self.db,
                run.child_session_id,
            )
        except (sqlite3.DatabaseError, psycopg.Error):
            logger.warning(
                "Database error loading active step workflow context for idle reprompt on "
                "run %s session %s",
                run.id,
                run.child_session_id,
                exc_info=True,
            )
            return IdleDetector.REPROMPT_MESSAGE
        except (json.JSONDecodeError, pydantic.ValidationError, TypeError, ValueError):
            logger.warning(
                "Malformed active step workflow context for idle reprompt on run %s session %s",
                run.id,
                run.child_session_id,
                exc_info=True,
            )
            return IdleDetector.REPROMPT_MESSAGE
        except Exception:
            logger.exception(
                "Unexpected error loading active step workflow context for idle reprompt "
                "on run %s session %s",
                run.id,
                run.child_session_id,
            )
            return IdleDetector.REPROMPT_MESSAGE
        if step_context is None:
            return IdleDetector.REPROMPT_MESSAGE

        message = (
            "Continue working on your task. Your active Gobby step workflow is not complete.\n"
            f"Workflow: {step_context.workflow_name}. Current step: {step_context.current_step}.\n"
        )
        if step_context.status_message:
            message = f"{message}{step_context.status_message.strip()}\n"
        return (
            f"{message}Finish the required Gobby lifecycle MCP transition, then call end_agent_run."
        )

    @staticmethod
    def _completed_codex_turn_recovery_due(
        snapshot: _CodexTranscriptSnapshot,
        *,
        idle_timeout_seconds: int,
    ) -> bool | None:
        if not snapshot.has_conclusive_task_complete:
            return None

        event = snapshot.lifecycle_event
        if event is None or event.timestamp is None:
            return None
        try:
            completed_at = parse_stored_datetime(event.timestamp)
        except (TypeError, ValueError):
            return None
        if completed_at is None:
            return None
        elapsed = (datetime.now(UTC) - completed_at).total_seconds()
        return elapsed >= idle_timeout_seconds

    async def _recover_reasoning_idle(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        session: Any | None,
        session_id: str | None,
        snapshot: _CodexTranscriptSnapshot | None = None,
    ) -> bool:
        """Interrupt a stale Codex reasoning turn and send a focused continuation."""
        if not self._tmux_config.reasoning_watchdog_interrupt_enabled:
            return False
        if session is None or getattr(session, "source", None) != "codex":
            return False

        if snapshot is None:
            transcript_path = getattr(session, "transcript_path", None)
            if not isinstance(transcript_path, str) or not transcript_path:
                return False
            try:
                snapshot = await self._read_codex_transcript_snapshot(transcript_path)
            except OSError:
                logger.warning(
                    "Failed to read Codex transcript for reasoning watchdog on run %s",
                    run.id,
                )
                return False

        if snapshot.latest_response_payload_type != "reasoning":
            return False

        logger.warning(
            "Codex reasoning watchdog interrupting run %s session %s: %s",
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

        self._idle_detector.record_reprompt(run.id)
        await self._record_watchdog_task_event(
            run,
            action="reasoning_interrupt",
            session_id=session_id,
            detail="latest_response_item=reasoning",
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
            return

        self._idle_detector.clear_state(run.id)
        await self._cleanup_handler.cleanup_agent(run, terminal_payload=f"Agent idle: {reason}")

    @staticmethod
    async def _read_codex_transcript_snapshot(
        transcript_path: str,
        *,
        limit: int = 8,
    ) -> _CodexTranscriptSnapshot:
        def _read() -> _CodexTranscriptSnapshot:
            items: deque[_CodexTranscriptEventSummary] = deque(maxlen=limit)
            lifecycle_event: _CodexTranscriptEventSummary | None = None
            task_started_event: _CodexTranscriptEventSummary | None = None
            capacity_error_event: _CodexTranscriptEventSummary | None = None
            latest_model_output_line_num: int | None = None
            last_malformed_line_num: int | None = None
            with open(transcript_path, encoding="utf-8") as handle:
                for line_num, raw_line in enumerate(handle, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        data = json.loads(raw_line)
                    except json.JSONDecodeError:
                        last_malformed_line_num = line_num
                        continue
                    if not isinstance(data, dict):
                        last_malformed_line_num = line_num
                        continue
                    event_type = data.get("type")
                    if not isinstance(event_type, str):
                        last_malformed_line_num = line_num
                        continue
                    if event_type not in {"response_item", "event_msg"}:
                        continue
                    payload = data.get("payload")
                    if not isinstance(payload, dict):
                        last_malformed_line_num = line_num
                        continue
                    payload_type = payload.get("type")
                    if not isinstance(payload_type, str):
                        last_malformed_line_num = line_num
                        continue
                    raw_timestamp = data.get("timestamp")
                    timestamp = raw_timestamp if isinstance(raw_timestamp, str) else None
                    summary = _CodexTranscriptEventSummary(
                        line_num=line_num,
                        timestamp=timestamp,
                        event_type=event_type,
                        payload_type=payload_type,
                    )
                    if event_type == "response_item":
                        items.append(summary)
                        if payload_type != "message" or payload.get("role") != "user":
                            latest_model_output_line_num = line_num
                    else:
                        if payload_type in {"agent_message", "agent_reasoning"}:
                            latest_model_output_line_num = line_num
                        if payload_type == "task_started":
                            task_started_event = summary
                        if payload_type in {"task_started", "task_complete"}:
                            lifecycle_event = summary
                        if (
                            payload_type == "error"
                            and payload.get("message") == _CODEX_MODEL_CAPACITY_MESSAGE
                            and payload.get("codex_error_info") == "server_overloaded"
                        ):
                            capacity_error_event = summary
            return _CodexTranscriptSnapshot(
                response_items=tuple(items),
                lifecycle_event=lifecycle_event,
                task_started_event=task_started_event,
                capacity_error_event=capacity_error_event,
                latest_model_output_line_num=latest_model_output_line_num,
                last_malformed_line_num=last_malformed_line_num,
            )

        return await asyncio.to_thread(_read)

    async def _log_codex_transcript_snapshot(
        self,
        run: AgentRun,
        *,
        reason: str,
        snapshot: _CodexTranscriptSnapshot | None = None,
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
                    "Failed to load session %s for Codex idle diagnostics on run %s",
                    session_id,
                    run.id,
                )
                return

            if session is None or getattr(session, "source", None) != "codex":
                return

            transcript_path = getattr(session, "transcript_path", None)
            if not isinstance(transcript_path, str) or not transcript_path:
                logger.warning(
                    "Codex idle diagnostic for run %s (%s): session %s has no transcript path",
                    run.id,
                    reason,
                    session_id,
                )
                return

            try:
                snapshot = await self._read_codex_transcript_snapshot(transcript_path)
            except OSError:
                logger.warning(
                    "Failed to read Codex transcript for idle diagnostic on run %s (%s)",
                    run.id,
                    reason,
                )
                return

        if not snapshot.response_items and snapshot.lifecycle_event is None:
            logger.warning(
                "Codex idle diagnostic for run %s (%s): no transcript summaries for session %s",
                run.id,
                reason,
                session_id,
            )
            return

        logger.warning(
            "Codex idle diagnostic for run %s (%s) session %s: %s",
            run.id,
            reason,
            session_id,
            json.dumps(snapshot.to_log_dict(), ensure_ascii=True),
        )
