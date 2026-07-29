"""Terminal prompt checks for tmux-backed agent runs."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from gobby.agents.loop_tracker import LoopTracker
from gobby.agents.prompt_detector import PromptDetector

if TYPE_CHECKING:
    from gobby.agents.tmux.session_manager import TmuxSessionManager
    from gobby.config.tmux import TmuxConfig
    from gobby.storage.agents import AgentRun

logger = logging.getLogger(__name__)

_VANISHED_TMUX_ERROR_MARKERS = (
    "can't find pane",
    "can't find session",
    "failed to connect to server",
    "no server running on",
)


def _is_expected_prompt_probe_error(error: Exception) -> bool:
    """Return whether a prompt probe failed because its tmux target vanished."""
    if isinstance(error, TimeoutError):
        return True
    message = str(error).casefold()
    if "no such file or directory" in message:
        return any(marker in message for marker in ("tmux-", "tmux socket", ".sock"))
    return any(marker in message for marker in _VANISHED_TMUX_ERROR_MARKERS)


def _log_prompt_probe_error(
    *,
    operation: str,
    run_id: str,
    tmux_target: str,
    error: Exception,
) -> None:
    """Log expected terminal races quietly and preserve unexpected tracebacks."""
    values = (
        operation,
        type(error).__name__,
        run_id,
        tmux_target,
        error,
    )
    if _is_expected_prompt_probe_error(error):
        logger.debug(
            "Prompt probe %s skipped: exception=%s run_id=%s tmux_target=%s error=%s",
            *values,
        )
        return
    logger.warning(
        "Prompt probe %s failed: exception=%s run_id=%s tmux_target=%s error=%s",
        *values,
        exc_info=True,
    )


class TerminalPromptMonitor:
    """Detect and dismiss blocking prompts in spawned agent tmux panes."""

    LOOP_PROMPT_DISMISS_MIN_INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        *,
        get_active_terminal_runs: Callable[[], list[AgentRun]],
        get_tmux: Callable[[], TmuxSessionManager],
        prompt_detector: PromptDetector,
        loop_tracker: LoopTracker,
        get_tmux_config: Callable[[], TmuxConfig],
        handle_looping_agent: Callable[[AgentRun], Awaitable[None]],
        on_prompt_injected: Callable[[AgentRun], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._get_active_terminal_runs = get_active_terminal_runs
        self._get_tmux = get_tmux
        self._prompt_detector = prompt_detector
        self._loop_tracker = loop_tracker
        self._get_tmux_config = get_tmux_config
        self._handle_looping_agent = handle_looping_agent
        self._on_prompt_injected = on_prompt_injected
        self._monotonic = monotonic
        self._last_enter_sent_at: dict[str, float] = {}
        self._last_loop_dismissed_at: dict[str, float] = {}
        self._run_db_callback = run_db

    async def _run_db(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._run_db_callback is None:
            return await asyncio.to_thread(func, *args, **kwargs)
        return await self._run_db_callback(func, *args, **kwargs)

    def mark_enter_sent(self, run_id: str) -> None:
        """Record that this run just received an automatic terminal keypress."""
        self._last_enter_sent_at[run_id] = self._monotonic()

    def clear(self, run_id: str) -> None:
        """Remove prompt-monitor state for a completed or cleaned-up run."""
        self._last_enter_sent_at.pop(run_id, None)
        self._last_loop_dismissed_at.pop(run_id, None)

    async def check_trust_prompts(self) -> int:
        """Check for folder trust prompts and auto-dismiss them."""
        runs = await self._run_db(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            detector = self._prompt_detector.for_provider(run.provider)
            if detector.was_dismissed(run.id):
                continue

            tmux_name = run.tmux_session_name
            if tmux_name is None:
                continue

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=15)
                if pane_output and detector.detect_trust_prompt(pane_output):
                    sent = await self._get_tmux().send_keys(
                        tmux_name,
                        PromptDetector.TRUST_DISMISS_KEYS,
                    )
                    if sent:
                        self.mark_enter_sent(run.id)
                        detector.mark_dismissed(run.id)
                        await self._notify_prompt_injected(run)
                        logger.info(
                            "Auto-dismissed trust prompt for agent %s (trust folder)",
                            run.id,
                        )
                        handled += 1
            except Exception as e:
                _log_prompt_probe_error(
                    operation="trust",
                    run_id=run.id,
                    tmux_target=tmux_name,
                    error=e,
                )

        return handled

    async def check_loop_prompts(self) -> int:
        """Check for loop detection prompts and auto-dismiss them."""
        runs = await self._run_db(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            detector = self._prompt_detector.for_provider(run.provider)
            tmux_name = run.tmux_session_name
            if tmux_name is None:
                continue

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=15)
                if not pane_output or not detector.detect_loop_prompt(pane_output):
                    continue
                if detector.was_loop_prompt_dismissed(run.id, pane_output):
                    continue

                now = self._monotonic()
                last_dismissed = self._last_loop_dismissed_at.get(run.id)
                if (
                    last_dismissed is not None
                    and now - last_dismissed < self.LOOP_PROMPT_DISMISS_MIN_INTERVAL_SECONDS
                ):
                    continue

                sent = await self._get_tmux().send_keys(
                    tmux_name,
                    PromptDetector.LOOP_DISMISS_KEYS,
                )
                if not sent:
                    continue

                self.mark_enter_sent(run.id)
                self._last_loop_dismissed_at[run.id] = now
                detector.mark_loop_prompt_dismissed(run.id, pane_output)
                count = self._loop_tracker.record_dismissal(run.id)
                logger.info(
                    "Auto-dismissed loop prompt for agent %s (%s/%s)",
                    run.id,
                    count,
                    self._loop_tracker.threshold,
                )
                handled += 1

                if self._loop_tracker.should_escalate(run.id):
                    logger.warning(
                        "Doom loop detected for agent %s: %s loop prompts dismissed, "
                        "escalating to kill",
                        run.id,
                        count,
                    )
                    await self._notify_looping_agent(run)
            except Exception as e:
                _log_prompt_probe_error(
                    operation="loop",
                    run_id=run.id,
                    tmux_target=tmux_name,
                    error=e,
                )

        return handled

    async def check_approval_prompts(self) -> int:
        """Check for approval prompts whose visible text says Enter approves."""
        if not self._get_tmux_config().auto_enter_approval_prompts:
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            detector = self._prompt_detector.for_provider(run.provider)
            tmux_name = run.tmux_session_name
            if tmux_name is None:
                continue

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=15)
                if not pane_output or not detector.detect_approval_prompt(pane_output):
                    continue
                if detector.was_approval_prompt_dismissed(run.id, pane_output):
                    continue

                sent = await self._get_tmux().send_keys(
                    tmux_name,
                    PromptDetector.ENTER_KEY,
                    literal=False,
                )
                if sent:
                    self.mark_enter_sent(run.id)
                    detector.mark_approval_prompt_dismissed(run.id, pane_output)
                    await self._notify_prompt_injected(run)
                    logger.info("Auto-entered approval prompt for agent %s", run.id)
                    handled += 1
            except Exception as e:
                _log_prompt_probe_error(
                    operation="approval",
                    run_id=run.id,
                    tmux_target=tmux_name,
                    error=e,
                )

        return handled

    async def check_queued_continuation_prompts(self) -> int:
        """Observe queued Gobby continuation prompts without editing the CLI input queue.

        This does not mutate the input queue or report a handled count; periodic
        enter handling owns submission. The wider 30-line pane capture is
        intentional because continuation text and the queued-message prompt can
        be separated by status/chrome lines.
        """
        if not self._get_tmux_config().auto_enter_agent_terminals:
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)

        for run in runs:
            detector = self._prompt_detector.for_provider(run.provider)
            tmux_name = run.tmux_session_name
            if tmux_name is None:
                continue

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=30)
                if not pane_output:
                    continue
                if not detector.detect_queued_continuation_prompt(pane_output):
                    continue

                # Observation-only: periodic enter handling owns any actual key submission.
                logger.info(
                    "Observed queued continuation prompt for agent %s; leaving input queue untouched",
                    run.id,
                )
            except Exception as e:
                _log_prompt_probe_error(
                    operation="queued-continuation",
                    run_id=run.id,
                    tmux_target=tmux_name,
                    error=e,
                )
        return 0

    async def check_periodic_enters(self) -> int:
        """Periodically send Enter to active spawned-agent terminal panes."""
        config = self._get_tmux_config()
        if not config.auto_enter_agent_terminals:
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)
        now = self._monotonic()
        interval = config.auto_enter_agent_interval_seconds

        handled = 0
        for run in runs:
            detector = self._prompt_detector.for_provider(run.provider)
            tmux_name = run.tmux_session_name
            if tmux_name is None:
                continue

            last_sent = self._last_enter_sent_at.get(run.id)
            if last_sent is not None and now - last_sent < interval:
                continue

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=15)
                if pane_output is None:
                    pane_output = ""
                if self._should_skip_periodic_enter_for_dialog(pane_output, config, detector):
                    logger.debug(
                        "Skipped periodic Enter for agent %s while known dialog is visible",
                        run.id,
                    )
                    continue

                sent = await self._get_tmux().send_keys(
                    tmux_name,
                    PromptDetector.ENTER_KEY,
                    literal=False,
                )
                if sent:
                    self._last_enter_sent_at[run.id] = now
                    logger.debug("Sent periodic Enter to agent terminal %s", run.id)
                    handled += 1
            except Exception as e:
                _log_prompt_probe_error(
                    operation="periodic-enter",
                    run_id=run.id,
                    tmux_target=tmux_name,
                    error=e,
                )

        return handled

    async def _notify_prompt_injected(self, run: AgentRun) -> None:
        callback = self._on_prompt_injected
        if callback is None:
            return
        try:
            await callback(run)
        except Exception:
            logger.warning(
                "Prompt-injection callback failed for agent %s",
                run.id,
                exc_info=True,
            )

    async def _notify_looping_agent(self, run: AgentRun) -> None:
        try:
            await self._handle_looping_agent(run)
        except Exception:
            logger.warning(
                "Loop-escalation callback failed for agent %s",
                run.id,
                exc_info=True,
            )

    def _should_skip_periodic_enter_for_dialog(
        self,
        pane_output: str,
        config: TmuxConfig,
        detector: PromptDetector,
    ) -> bool:
        """Return True when periodic Enter would confirm a known prompt dialog."""
        if not pane_output:
            return False

        if detector.detect_trust_prompt(pane_output):
            return True
        if detector.detect_loop_prompt(pane_output):
            return True
        if not config.auto_enter_approval_prompts and detector.detect_approval_prompt(pane_output):
            return True
        return False
