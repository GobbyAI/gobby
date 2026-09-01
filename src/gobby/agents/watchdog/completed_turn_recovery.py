"""Bounded completed-turn recovery orchestration for the idle watchdog."""

import logging
from datetime import UTC, datetime
from typing import Literal, Protocol

from gobby.agents.watchdog.models import (
    CompletedTurnRecoveryState,
    WatchdogTranscriptSnapshot,
)
from gobby.config.tmux import TmuxConfig
from gobby.storage.agents import AgentRun
from gobby.workflows.step_context import StepWorkflowContext

CompletedTurnRecoveryDecision = Literal["duplicate", "recover", "exhausted"]
logger = logging.getLogger(__name__)


class CompletedTurnRecoveryHost(Protocol):
    _completed_turn_recovery: dict[str, CompletedTurnRecoveryState]
    _tmux_config: TmuxConfig

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
) -> int:
    event = snapshot.latest_turn_event
    if event is None or event.timestamp is None:
        return 0

    step_context, lookup_succeeded = await host._load_step_workflow_context(run)
    if (
        lookup_succeeded
        and step_context is not None
        and step_context.is_entry_step
        and step_requires_gobby_proxy(step_context)
        and await host._session_made_successful_mcp_call(run) is False
    ):
        # The workflow's entry step admits only Gobby MCP proxy tools and the
        # session has never completed one — the tools were almost certainly
        # never registered in the provider runtime (e.g. Codex's MCP startup
        # timeout), so no number of reprompts can produce workflow progress.
        # Later MCP-only steps are excluded: a session can legitimately reach
        # them with zero MCP calls when earlier steps used native tools, and
        # there a reprompt can still help.
        reason = (
            "Gobby MCP proxy tools unavailable: session made no successful Gobby MCP "
            f"call while pinned in MCP-only entry step '{step_context.current_step}' "
            "(likely stdio bridge startup failure)"
        )
        logger.error("Failing idle agent %s without reprompts: %s", run.id, reason)
        await host._log_transcript_snapshot(
            run,
            reason="failing toolless run pinned in MCP-only step",
            snapshot=snapshot,
            level=logging.ERROR,
        )
        await host._fail_idle_agent(run, reason=reason)
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
        return 0
    if decision == "exhausted":
        # A run parked on a satisfied exit condition, or whose task was closed
        # or handed back, has no progress left to make, so failing it would
        # report finished work as an error.
        if await host._complete_if_work_finished(run):
            await host._log_transcript_snapshot(
                run,
                reason="completing idle agent whose work already finished",
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
    state.successful_reprompts += 1
    await host._record_watchdog_task_event(
        run,
        action="completed_turn_reprompt",
        session_id=session_id,
        detail="latest_turn_kind=completed",
    )
    return 1


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
