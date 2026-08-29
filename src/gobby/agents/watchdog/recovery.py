from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, cast

import psycopg
import pydantic

from gobby.agents.capture import terminate_managed_runtime_async
from gobby.agents.idle_detector import IdleDetector
from gobby.agents.watchdog.completed_turn_recovery import (
    format_reprompt_message,
    recover_completed_turn,
)
from gobby.agents.watchdog.models import CapacityRecoveryState, CompletedTurnRecoveryState
from gobby.storage.terminals import Terminal
from gobby.terminals.error_classification import is_vanished_terminal_target
from gobby.terminals.runtime import Delivered, TerminalWriteError
from gobby.terminals.write_coordinator import WriteCoordinator, WriteRequest
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_context import (
    StepWorkflowContext,
    first_incomplete_step_workflow,
    get_active_step_workflow_context,
)

if TYPE_CHECKING:
    from gobby.agents.agent_cleanup import AgentCleanupHandler
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.agents.watchdog import TranscriptWatchdogReader, WatchdogReaderRegistry
    from gobby.agents.watchdog.models import WatchdogTranscriptSnapshot
    from gobby.agents.watchdog.transcript_resolver import WatchdogTranscriptResolver
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun, LocalAgentRunManager, TerminalAction
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


class WatchdogRecoveryCoordinator:
    """Coordinate watchdog recovery policies and terminal cleanup."""

    def __init__(
        self,
        *,
        agent_run_manager: LocalAgentRunManager,
        db: HubDatabase,
        get_session_manager: Callable[[], SessionManager | None],
        tmux: TmuxSessionManager,
        idle_detector: IdleDetector,
        watchdog_readers: WatchdogReaderRegistry,
        cleanup_handler: AgentCleanupHandler,
        tmux_config: TmuxConfig,
        transcript_resolver: WatchdogTranscriptResolver,
        run_db: Callable[..., Awaitable[Any]],
        task_manager: LocalTaskManager | None = None,
        terminal_services: Any | None = None,
    ) -> None:
        self._agent_run_manager = agent_run_manager
        self.db = db
        self._get_session_manager = get_session_manager
        self._tmux = tmux
        self._idle_detector = idle_detector
        self._watchdog_readers = watchdog_readers
        self._cleanup_handler = cleanup_handler
        self._tmux_config = tmux_config
        self._transcript_resolver = transcript_resolver
        self._run_db = run_db
        self._task_manager = task_manager
        self._terminal_services = terminal_services
        self._capacity_recovery: dict[str, CapacityRecoveryState] = {}
        self._completed_turn_recovery: dict[str, CompletedTurnRecoveryState] = {}
        self._reprompt_delivery_failures: dict[str, int] = {}

    def clear(self) -> None:
        self._capacity_recovery.clear()
        self._completed_turn_recovery.clear()
        self._reprompt_delivery_failures.clear()
        self._transcript_resolver.clear()

    def prune(self, active_run_ids: set[str]) -> None:
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
        self._reprompt_delivery_failures = {
            run_id: failures
            for run_id, failures in self._reprompt_delivery_failures.items()
            if run_id in active_run_ids
        }
        self._transcript_resolver.prune(active_run_ids)

    def discard(self, run_id: str) -> None:
        self._capacity_recovery.pop(run_id, None)
        self._completed_turn_recovery.pop(run_id, None)
        self._reprompt_delivery_failures.pop(run_id, None)
        self._transcript_resolver.discard(run_id)

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

    async def recover_completed_turn(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        session_id: str | None,
        transcript_path: str,
        snapshot: WatchdogTranscriptSnapshot,
    ) -> int:
        return await recover_completed_turn(
            self,
            run,
            tmux_name=tmux_name,
            session_id=session_id,
            transcript_path=transcript_path,
            snapshot=snapshot,
        )

    def _write_target(self, run: AgentRun) -> tuple[Terminal, WriteCoordinator] | None:
        services = self._terminal_services
        if services is None or services.coordinator is None:
            return None
        terminal = services.terminal_for(run)
        if terminal is None:
            return None
        return terminal, services.coordinator

    async def _deliver(
        self,
        coordinator: WriteCoordinator,
        terminal_id: str,
        action_key: str,
        steps: list[tuple[Literal["text", "key"], str]],
    ) -> bool:
        """Write one automatic action; False when any step did not definitively land."""
        requests = [
            WriteRequest(
                terminal_id=terminal_id,
                action_key=action_key,
                origin="automatic",
                kind=kind,
                payload=payload,
            )
            for kind, payload in steps
        ]
        try:
            if len(requests) == 1:
                outcome = await coordinator.write(requests[0])
            else:
                outcome = await coordinator.run_sequence(
                    terminal_id,
                    action_key=action_key,
                    origin="automatic",
                    steps=requests,
                )
        except TerminalWriteError as exc:
            if is_vanished_terminal_target(exc):
                logger.debug("Terminal write %s skipped: target gone: %s", action_key, exc)
            else:
                logger.warning("Terminal write %s failed: %s", action_key, exc, exc_info=True)
            return False
        return isinstance(outcome, Delivered)

    async def _recover_failed_reprompt_clear(self, run: AgentRun, tmux_name: str) -> bool:
        del tmux_name
        services = self._terminal_services
        if services is None or not await services.is_live(run):
            logger.debug(
                "Cannot recover failed idle prompt clear for agent %s: terminal gone",
                run.id,
            )
            return False
        target = self._write_target(run)
        if target is None:
            return False
        terminal, coordinator = target
        interrupted = await self._deliver(
            coordinator,
            terminal.id,
            f"watchdog-interrupt:{run.id}",
            [("text", "\x03"), ("key", "enter")],
        )
        if not interrupted:
            logger.debug("Failed to interrupt queued prompt while recovering agent %s", run.id)
            return False
        if not await services.is_live(run):
            logger.debug("Cannot reprompt agent %s after recovery: terminal gone", run.id)
            return False
        return True

    async def _send_idle_reprompt(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        reprompt_message: str | None = None,
    ) -> bool:
        """Reprompt an idle agent, bounding how often delivery may fail.

        Every other give-up counter advances only on success, so a terminal
        refusing all automatic writes bypasses all of them and retries
        forever. This is the one boundary the three re-arming callers share.
        """
        delivered = await self._attempt_idle_reprompt(
            run,
            tmux_name=tmux_name,
            reprompt_message=reprompt_message,
        )
        if delivered:
            self._reprompt_delivery_failures.pop(run.id, None)
            return True

        failures = self._reprompt_delivery_failures.get(run.id, 0) + 1
        self._reprompt_delivery_failures[run.id] = failures
        # Reuses max_reprompt_attempts as the bound VALUE while keeping its own
        # counter: successful_reprompts is the "agent isn't progressing" budget,
        # and spending it here would misreport an unreachable agent as a lazy one.
        max_attempts = self._tmux_config.max_reprompt_attempts
        if failures < max_attempts:
            logger.warning(
                "Idle reprompt for agent %s was not delivered (%s/%s)",
                run.id,
                failures,
                max_attempts,
            )
            return False
        logger.error(
            "Failing agent %s: idle reprompt undeliverable after %s attempts",
            run.id,
            failures,
        )
        await self._fail_idle_agent(
            run,
            reason=f"idle reprompt undeliverable after {failures} attempts",
        )
        return False

    async def _attempt_idle_reprompt(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        reprompt_message: str | None = None,
    ) -> bool:
        if reprompt_message is None:
            reprompt_message = await self._idle_reprompt_message(run)
        target = self._write_target(run)
        if target is None:
            return False
        terminal, coordinator = target
        cleared = await self._deliver(
            coordinator,
            terminal.id,
            f"idle-reprompt-clear:{run.id}",
            [("key", "escape")],
        )
        if not cleared:
            logger.debug("Failed to clear queued prompt before reprompting agent %s", run.id)
            if not await self._recover_failed_reprompt_clear(run, tmux_name):
                return False
        # The emptied composer settles an earlier reprompt whose Enter never
        # resolved; left latched, it would suppress every later reprompt.
        coordinator.observe_resolved(terminal.id, f"idle-reprompt:{run.id}")
        sent = await self._deliver(
            coordinator,
            terminal.id,
            f"idle-reprompt:{run.id}",
            [("text", reprompt_message), ("key", "enter")],
        )
        if not sent:
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

    async def _session_made_successful_mcp_call(self, run: AgentRun) -> bool | None:
        """Whether the run's child session ever completed a successful Gobby MCP call.

        Reads the session's ``mcp_calls`` variable, which the workflow observer
        records on every successful proxied call. Returns None when the lookup
        fails so callers fail open to the reprompt path.
        """
        session_id = run.child_session_id
        if not session_id:
            return None
        try:
            variables = await self._run_db(
                SessionVariableManager(self.db).get_variables,
                session_id,
            )
        except psycopg.DatabaseError:
            logger.warning(
                "Database error loading session variables for MCP-availability check on "
                "run %s session %s",
                run.id,
                session_id,
                exc_info=True,
            )
            return None
        if not isinstance(variables, dict):
            return None
        return bool(variables.get("mcp_calls"))

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

    async def _recover_reasoning_idle(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        session: Session | None,
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
            transcript_path = await self._transcript_resolver.resolve(session, run_id=run.id)
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

        from gobby.terminals.runtime import Delivered
        from gobby.terminals.write_coordinator import SequenceDelay, WriteRequest

        terminal = (
            None if self._terminal_services is None else self._terminal_services.terminal_for(run)
        )
        coordinator = (
            None if self._terminal_services is None else self._terminal_services.coordinator
        )
        if terminal is None or coordinator is None:
            return False
        settle_seconds = self._tmux_config.reasoning_watchdog_settle_seconds
        steps: list[WriteRequest | SequenceDelay] = [
            WriteRequest(
                terminal_id=terminal.id,
                action_key=f"reasoning-interrupt:{run.id}",
                origin="automatic",
                kind="text",
                payload="\x03",
            )
        ]
        if settle_seconds:
            steps.append(SequenceDelay(seconds=settle_seconds))
        steps.extend(
            [
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key=f"reasoning-interrupt:{run.id}",
                    origin="automatic",
                    kind="text",
                    payload=REASONING_WATCHDOG_CONTINUATION,
                ),
                WriteRequest(
                    terminal_id=terminal.id,
                    action_key=f"reasoning-interrupt:{run.id}",
                    origin="automatic",
                    kind="key",
                    payload="enter",
                ),
            ]
        )
        outcome = await coordinator.run_sequence(
            terminal.id,
            action_key=f"reasoning-interrupt:{run.id}",
            origin="automatic",
            steps=steps,
        )
        if not isinstance(outcome, Delivered):
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
        except (psycopg.Error, ValueError):
            logger.warning(
                "Failed to load task %s for idle watchdog audit on run %s",
                run.task_id,
                run.id,
                exc_info=True,
            )
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
        except (psycopg.Error, ValueError):
            logger.warning(
                "Failed to record idle watchdog audit for run %s task %s",
                run.id,
                run.task_id,
                exc_info=True,
            )

    async def _fail_idle_agent(self, run: AgentRun, reason: str) -> None:
        """Fail an agent that is irrecoverably idle."""
        payload = f"Agent idle: {reason}"

        async def terminalize(
            _action: TerminalAction,
            captured: str | None,
        ) -> AgentRun | None:
            await self._cleanup_handler.cleanup_agent(
                run,
                terminal_payload=captured or payload,
            )
            return cast(
                "AgentRun | None",
                await self._run_db(self._agent_run_manager.get, run.id),
            )

        await self._terminalize_idle_agent(
            run,
            action="fail",
            payload=payload,
            terminalize=terminalize,
        )

    async def _terminalize_idle_agent(
        self,
        run: AgentRun,
        *,
        action: TerminalAction,
        payload: str,
        terminalize: Callable[[TerminalAction, str | None], Awaitable[AgentRun | None]],
    ) -> None:
        """Capture-then-kill the run's active terminal, or terminalize directly without one."""
        services = self._terminal_services
        terminal = None if services is None else services.terminal_for(run)
        if services is None or terminal is None:
            self._idle_detector.clear_state(run.id)
            await terminalize(action, None)
            self.discard(run.id)
            return

        result = await terminate_managed_runtime_async(
            storage=self._agent_run_manager,
            run=run,
            terminal=terminal,
            runtime=services.runtime_for(terminal),
            action=action,
            reason=payload,
            terminalize=terminalize,
        )
        if not result.success:
            logger.warning(
                "Idle-agent %s failed for run %s: %s (%s)",
                action,
                run.id,
                result.error,
                result.error_code,
            )
            return

        self._idle_detector.clear_state(run.id)
        self.discard(run.id)

    async def _complete_if_step_workflow_finished(self, run: AgentRun) -> bool:
        """Complete an idle agent whose step workflow already reached its exit condition.

        Every bundled agent's terminate step allows only the four gobby MCP
        proxy tools, so an agent whose proxy never started has no permitted
        action left once the workflow is over. Failing it there would report
        finished work as an error (#19097). Returns whether the run was
        terminalized as a completion.
        """
        step_context, _lookup_succeeded = await self._load_step_workflow_context(run)
        if step_context is None or not await self._step_workflow_exit_condition_met(run):
            return False

        logger.info(
            "Agent %s is idle at step %s/%s with its exit condition already satisfied — "
            "completing instead of failing",
            run.id,
            step_context.workflow_name,
            step_context.current_step,
        )
        await self._complete_idle_agent(
            run,
            reason=(
                f"step workflow '{step_context.workflow_name}' reached its exit condition at "
                f"step '{step_context.current_step}' but the agent never called end_agent_run"
            ),
        )
        return True

    async def _complete_idle_agent(self, run: AgentRun, reason: str) -> None:
        """Complete an idle agent whose step workflow already finished."""
        payload = f"Agent completed by watchdog: {reason}"

        async def terminalize(
            _action: TerminalAction,
            captured: str | None,
        ) -> AgentRun | None:
            await self._cleanup_handler.cleanup_agent(
                run,
                terminal_payload=captured or payload,
                is_success=True,
            )
            return cast(
                "AgentRun | None",
                await self._run_db(self._agent_run_manager.get, run.id),
            )

        await self._terminalize_idle_agent(
            run,
            action="complete",
            payload=payload,
            terminalize=terminalize,
        )

    async def _step_workflow_exit_condition_met(self, run: AgentRun) -> bool:
        """Return whether every active step workflow on the child session is finished."""
        session_id = run.child_session_id
        if not session_id:
            return False
        try:
            incomplete = await self._run_db(
                first_incomplete_step_workflow,
                self.db,
                session_id,
            )
        except Exception:
            logger.exception(
                "Failed to evaluate step workflow completion for idle run %s session %s",
                run.id,
                session_id,
            )
            return False
        return incomplete is None

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

            provider_id = session.source or run.provider
            reader = self._watchdog_readers.for_provider(provider_id)
            if reader is None:
                return
            transcript_path = await self._transcript_resolver.resolve(session, run_id=run.id)
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
