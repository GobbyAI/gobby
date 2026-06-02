from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg
import pydantic

from gobby.agents.idle_detector import IdleDetector
from gobby.agents.prompt_detector import PromptDetector
from gobby.utils.datetime import parse_stored_datetime
from gobby.workflows.step_context import get_active_step_workflow_context

if TYPE_CHECKING:
    from gobby.agents.agent_cleanup import AgentCleanupHandler
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun, LocalAgentRunManager
    from gobby.storage.hub.protocol import HubDatabase
    from gobby.storage.sessions import SessionManager
    from gobby.storage.tasks import LocalTaskManager

logger = logging.getLogger(__name__)
WATCHDOG_ACTOR = "agent_idle_watchdog"
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
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)

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
        session_id = run.child_session_id or run.parent_session_id
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
                            self._idle_detector.reset_idle(run.id)
                            return 0
                        else:
                            session_stale = True
                except (ValueError, TypeError):
                    pass

        pane_output = await self._tmux.capture_pane(tmux_name, lines=15)
        if pane_output is None and not session_stale:
            return 0

        queued_message_prompt_visible = False
        if pane_output is not None:
            status = self._idle_detector.detect(pane_output)
            queued_message_prompt_visible = self._prompt_detector.detect_queued_message_prompt(
                pane_output
            )

            if status == "context_full":
                logger.info(f"Agent {run.id} hit context window limit - failing")
                await self._fail_idle_agent(run, reason="context window exhausted")
                return 1

            if not session_stale and status == "active":
                self._idle_detector.reset_idle(run.id)
                return 0

            if self._idle_detector.has_unsubmitted_input(pane_output):
                logger.info(
                    "Agent %s has unsubmitted prompt input visible; skipping idle reprompt",
                    run.id,
                )
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
                f"Agent {run.id} still idle after "
                f"{self._tmux_config.max_reprompt_attempts} reprompts — failing"
            )
            await self._log_recent_codex_response_items(
                run,
                reason="failing after max idle reprompts",
            )
            await self._fail_idle_agent(run, reason="idle after max reprompt attempts")
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
            ):
                return 1

            logger.info(f"Reprompting idle agent {run.id}")
            await self._log_recent_codex_response_items(
                run,
                reason="reprompting apparently idle agent",
            )
            reprompt_message = await self._idle_reprompt_message(run)
            cleared = await self._tmux.send_keys(tmux_name, "Escape", literal=False)
            if not cleared:
                logger.warning("Failed to clear queued prompt before reprompting agent %s", run.id)
                return 0
            sent = await self._tmux.send_keys(tmux_name, reprompt_message)
            if not sent:
                logger.warning("Failed to send idle reprompt text to agent %s", run.id)
                return 0
            submitted = await self._tmux.send_keys(tmux_name, "Enter", literal=False)
            if submitted:
                self._idle_detector.record_reprompt(run.id)
                return 1
            else:
                logger.warning("Failed to submit idle reprompt for agent %s", run.id)
            return 0

        return 0

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
    def _latest_response_payload_type(items: list[dict[str, object]]) -> str | None:
        for item in reversed(items):
            payload_type = item.get("payload_type")
            if isinstance(payload_type, str) and payload_type:
                return payload_type
        return None

    async def _recover_reasoning_idle(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        session: Any | None,
        session_id: str | None,
    ) -> bool:
        """Interrupt a stale Codex reasoning turn and send a focused continuation."""
        if not self._tmux_config.reasoning_watchdog_interrupt_enabled:
            return False
        if session is None or getattr(session, "source", None) != "codex":
            return False

        transcript_path = getattr(session, "transcript_path", None)
        if not isinstance(transcript_path, str) or not transcript_path:
            return False

        try:
            items = await self._read_recent_codex_response_items(transcript_path)
        except OSError as exc:
            logger.warning(
                "Failed to read Codex transcript for reasoning watchdog on run %s: %s",
                run.id,
                exc,
            )
            return False

        if self._latest_response_payload_type(items) != "reasoning":
            return False

        redacted_items = [
            {
                "line_num": item.get("line_num"),
                "timestamp": item.get("timestamp"),
                "payload_type": item.get("payload_type"),
            }
            for item in items
        ]
        logger.warning(
            "Codex reasoning watchdog interrupting run %s session %s: %s",
            run.id,
            session_id,
            json.dumps(redacted_items, ensure_ascii=True),
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
            await self._tmux.kill_session(run.tmux_session_name)
            await self._clear_tmux_session_name(run)

        self._idle_detector.clear_state(run.id)
        await self._cleanup_handler.cleanup_agent(run, terminal_payload=f"Agent idle: {reason}")

    @staticmethod
    async def _read_recent_codex_response_items(
        transcript_path: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        def _read() -> list[dict[str, object]]:
            items: deque[dict[str, object]] = deque(maxlen=limit)
            with open(transcript_path, encoding="utf-8") as handle:
                for line_num, raw_line in enumerate(handle, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        data = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict) or data.get("type") != "response_item":
                        continue
                    payload = data.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    items.append(
                        {
                            "line_num": line_num,
                            "timestamp": data.get("timestamp"),
                            "payload_type": payload.get("type"),
                            "raw": data,
                        }
                    )
            return list(items)

        return await asyncio.to_thread(_read)

    async def _log_recent_codex_response_items(self, run: AgentRun, *, reason: str) -> None:
        session_manager = self._get_session_manager()
        if session_manager is None:
            return

        session_id = run.child_session_id or run.parent_session_id
        if not session_id:
            return

        try:
            session = await self._run_db(session_manager.get, session_id)
        except Exception as exc:
            logger.warning(
                "Failed to load session %s for Codex idle diagnostics on run %s: %s",
                session_id,
                run.id,
                exc,
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
            items = await self._read_recent_codex_response_items(transcript_path)
        except OSError as exc:
            logger.warning(
                "Failed to read Codex transcript for idle diagnostic on run %s (%s): %s",
                run.id,
                reason,
                exc,
            )
            return

        if not items:
            logger.warning(
                "Codex idle diagnostic for run %s (%s): no recent response_item records for session %s",
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
            json.dumps(items, ensure_ascii=True),
        )
