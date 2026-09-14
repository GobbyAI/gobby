"""Bounded completed-turn recovery orchestration for the idle watchdog."""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from gobby.agents.watchdog.models import (
    CompletedTurnRecoveryState,
    WatchdogTranscriptSnapshot,
)
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_context import StepWorkflowContext

CompletedTurnRecoveryDecision = Literal["duplicate", "recover", "exhausted"]
logger = logging.getLogger(__name__)


class CompletedTurnRecoveryHost(Protocol):
    _completed_turn_recovery: dict[str, CompletedTurnRecoveryState]
    _tmux_config: TmuxConfig
    db: HubDatabase
    _run_db: Callable[..., Awaitable[Any]]

    async def _load_step_workflow_context(
        self,
        run: AgentRun,
    ) -> tuple[StepWorkflowContext | None, bool]: ...

    async def _idle_reprompt_message(
        self,
        run: AgentRun,
        *,
        step_context: StepWorkflowContext | None = None,
        context_resolved: bool = False,
    ) -> str: ...

    async def _send_idle_reprompt(
        self,
        run: AgentRun,
        *,
        tmux_name: str,
        reprompt_message: str | None = None,
    ) -> bool: ...

    async def _record_watchdog_task_event(
        self,
        run: AgentRun,
        *,
        action: str,
        session_id: str | None,
        detail: str,
    ) -> None: ...

    async def _fail_idle_agent(self, run: AgentRun, reason: str) -> None: ...

    async def _session_made_successful_mcp_call(self, run: AgentRun) -> bool | None: ...

    async def _complete_if_work_finished(self, run: AgentRun) -> bool: ...

    async def _complete_idle_agent(self, run: AgentRun, reason: str) -> None: ...

    async def _log_transcript_snapshot(
        self,
        run: AgentRun,
        *,
        reason: str,
        snapshot: WatchdogTranscriptSnapshot | None = None,
        level: int = logging.WARNING,
    ) -> None: ...


_GOBBY_PROXY_TOOL_PREFIX = "mcp__gobby__"


def step_requires_gobby_proxy(step_context: StepWorkflowContext | None) -> bool:
    """True when the current step's only allowed tools are Gobby MCP proxy tools."""
    if step_context is None:
        return False
    allowed = step_context.allowed_tools
    if not isinstance(allowed, list) or not allowed:
        return False
    return all(tool.startswith(_GOBBY_PROXY_TOOL_PREFIX) for tool in allowed)


def step_progress_requires_gobby_mcp(step_context: StepWorkflowContext | None) -> bool:
    """True when the step cannot advance without a successful Gobby MCP call.

    Two shapes qualify. A step whose allowed tools are Gobby proxy tools only,
    and a step whose sole declared route forward is an ``on_mcp_success``
    handler — the latter can permit every native tool and still be unable to
    progress, which is how `task-close-validator` (`allowed_tools: all`, exit
    condition set only by `gobby-agents:end_agent_run`) wedges.
    """
    if step_context is None:
        return False
    return step_requires_gobby_proxy(step_context) or step_context.mcp_progress_only


def codex_mcp_startup_error(pane_tail: str | None) -> str | None:
    """Return the latest Codex MCP startup diagnostic, retaining wrapped cause text."""
    if pane_tail is None:
        return None
    paragraphs: list[str] = re.split(r"\n\s*\n", pane_tail)
    for paragraph in reversed(paragraphs):
        # Codex's required-server error can wrap inside the identifying phrase
        # and the handshake cause. Blank lines separate it from other output.
        diagnostic = " ".join(paragraph.split())
        if "required MCP servers failed to initialize:" in diagnostic:
            return diagnostic
        for raw_line in reversed(paragraph.splitlines()):
            line = raw_line.strip()
            if "MCP client for" in line and "failed to start" in line:
                return line
    return None


def workflow_fingerprint(
    run_id: str,
    step_context: StepWorkflowContext | None,
    *,
    lookup_succeeded: bool,
) -> str | None:
    if not lookup_succeeded:
        return None
    if step_context is None:
        return run_id
    return f"{step_context.workflow_name}:{step_context.current_step}"


def evaluate_completed_turn(
    state: CompletedTurnRecoveryState,
    *,
    fingerprint: str | None,
    identity: tuple[str, int, datetime],
    max_attempts: int,
) -> CompletedTurnRecoveryDecision:
    if state.last_completion_identity == identity:
        return "duplicate"
    if fingerprint is not None:
        if state.workflow_fingerprint is not None and state.workflow_fingerprint != fingerprint:
            state.successful_reprompts = 0
        state.workflow_fingerprint = fingerprint
    if state.successful_reprompts >= max_attempts:
        return "exhausted"
    return "recover"


def completed_turn_recovery_due(
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


async def recover_completed_turn(
    host: CompletedTurnRecoveryHost,
    run: AgentRun,
    *,
    tmux_name: str,
    session_id: str | None,
    transcript_path: str,
    snapshot: WatchdogTranscriptSnapshot,
    idle_timeout_seconds: int,
    pane_tail: str | None = None,
) -> int:
    event = snapshot.latest_turn_event
    if event is None or event.timestamp is None:
        return 0

    step_context, lookup_succeeded = await host._load_step_workflow_context(run)
    if (
        lookup_succeeded
        and step_context is not None
        and step_progress_requires_gobby_mcp(step_context)
        and await host._session_made_successful_mcp_call(run) is False
    ):
        # This workflow step cannot advance without a Gobby MCP call and the
        # session has never completed one, so no number of reprompts can produce
        # workflow progress.
        reason = (
            "Gobby MCP proxy tools unavailable: session made no successful Gobby MCP "
            f"call while pinned in MCP-gated step '{step_context.current_step}' "
            "(likely stdio bridge startup failure)"
        )
        startup_error = codex_mcp_startup_error(pane_tail)
        if startup_error is not None:
            reason = f"{reason}; provider startup error: {startup_error}"
        logger.error("Failing idle agent %s without reprompts: %s", run.id, reason)
        await host._log_transcript_snapshot(
            run,
            reason="failing run pinned in MCP-gated step with no successful MCP call",
            snapshot=snapshot,
            level=logging.ERROR,
        )
        await host._fail_idle_agent(run, reason=reason)
        return 1

    if await host._complete_if_work_finished(run):
        await host._log_transcript_snapshot(
            run,
            reason="completing idle agent whose work already finished",
            snapshot=snapshot,
            level=logging.INFO,
        )
        return 1

    fingerprint = workflow_fingerprint(
        run.id,
        step_context,
        lookup_succeeded=lookup_succeeded,
    )
    identity = (transcript_path, event.line_num, event.timestamp)
    state = host._completed_turn_recovery.setdefault(
        run.id,
        CompletedTurnRecoveryState(workflow_fingerprint=fingerprint),
    )
    max_attempts = host._tmux_config.max_reprompt_attempts
    decision = evaluate_completed_turn(
        state,
        fingerprint=fingerprint,
        identity=identity,
        max_attempts=max_attempts,
    )
    if decision == "duplicate":
        return await _give_up_unanswered_reprompt(
            host,
            run,
            state=state,
            snapshot=snapshot,
            idle_timeout_seconds=idle_timeout_seconds,
        )
    if decision == "exhausted":
        if await _complete_exhausted_unbound_run(host, run):
            await host._log_transcript_snapshot(
                run,
                reason="completing unbound agent after max completed-turn reprompts",
                snapshot=snapshot,
                level=logging.INFO,
            )
            return 1
        logger.error(
            "Agent %s completed another turn without workflow progress after %s recovery "
            "reprompts — failing",
            run.id,
            max_attempts,
        )
        await host._log_transcript_snapshot(
            run,
            reason="failing after max completed-turn recovery reprompts",
            snapshot=snapshot,
            level=logging.ERROR,
        )
        await host._fail_idle_agent(
            run,
            reason="completed turns without workflow progress after max reprompt attempts",
        )
        return 1

    logger.info("Recovering completed turn for idle agent %s", run.id)
    await host._log_transcript_snapshot(
        run,
        reason="recovering completed turn",
        snapshot=snapshot,
        level=logging.INFO,
    )
    reprompt_message = await host._idle_reprompt_message(
        run,
        step_context=step_context,
        context_resolved=True,
    )
    if not await host._send_idle_reprompt(
        run,
        tmux_name=tmux_name,
        reprompt_message=reprompt_message,
    ):
        return 0
    state.last_completion_identity = identity
    state.last_reprompt_at = datetime.now(UTC)
    state.successful_reprompts += 1
    await host._record_watchdog_task_event(
        run,
        action="completed_turn_reprompt",
        session_id=session_id,
        detail="latest_turn_kind=completed",
    )
    return 1


async def _give_up_unanswered_reprompt(
    host: CompletedTurnRecoveryHost,
    run: AgentRun,
    *,
    state: CompletedTurnRecoveryState,
    snapshot: WatchdogTranscriptSnapshot,
    idle_timeout_seconds: int,
) -> int:
    """Stop waiting on a delivered reprompt that never produced another turn.

    The transcript identity still matches the turn the watchdog already
    answered, so the provider session never started a new turn (a dead
    app-server or a wedged TUI). Waiting on it forever holds the run, its
    terminal, and its worktree; bound it by the idle timeout that declared
    the turn idle in the first place.
    """
    if state.last_reprompt_at is None:
        return 0
    elapsed = (datetime.now(UTC) - state.last_reprompt_at).total_seconds()
    if elapsed < idle_timeout_seconds:
        return 0
    logger.error(
        "Agent %s started no new turn in %.0fs after its completed-turn reprompt — failing",
        run.id,
        elapsed,
    )
    await host._log_transcript_snapshot(
        run,
        reason="failing after unanswered completed-turn reprompt",
        snapshot=snapshot,
        level=logging.ERROR,
    )
    await host._fail_idle_agent(
        run,
        reason=(
            f"no new turn after completed-turn reprompt within {idle_timeout_seconds}s idle timeout"
        ),
    )
    return 1


async def _complete_exhausted_unbound_run(host: CompletedTurnRecoveryHost, run: AgentRun) -> bool:
    """Complete a responsive run with no lifecycle obligation once reprompts are exhausted.

    Every exhausted reprompt drew a completed turn, so the agent is alive. With
    no step workflow, bound task, or claimed task, reprompts have nothing to
    drive; the agent only never called end_agent_run, so failing it would
    report finished work as an error. A run whose obligations cannot be proven
    absent keeps failing. Returns whether the run was completed.
    """
    if run.task_id is not None or not run.child_session_id:
        return False
    step_context, lookup_succeeded = await host._load_step_workflow_context(run)
    if not lookup_succeeded or step_context is not None:
        return False
    try:
        variables = await host._run_db(
            SessionVariableManager(host.db).get_variables,
            run.child_session_id,
        )
    except Exception:
        logger.warning(
            "Failed to read claimed tasks for exhausted idle agent %s",
            run.id,
            exc_info=True,
        )
        return False
    if variables.get("claimed_tasks"):
        return False
    logger.info(
        "Agent %s has no step workflow or task after max idle reprompts — completing "
        "instead of failing",
        run.id,
    )
    await host._complete_idle_agent(
        run,
        reason=(
            "no step workflow or task remained after max idle reprompts but the agent "
            "never called end_agent_run"
        ),
    )
    return True


def format_reprompt_message(
    step_context: StepWorkflowContext | None,
    *,
    fallback_message: str,
) -> str:
    if step_context is None:
        return fallback_message

    message = (
        "Continue working on your task. Your active Gobby step workflow is not complete.\n"
        f"Workflow: {step_context.workflow_name}. Current step: {step_context.current_step}.\n"
    )
    if step_context.status_message:
        message = f"{message}{step_context.status_message.strip()}\n"
    return f"{message}Finish the required Gobby lifecycle MCP transition, then call end_agent_run."
