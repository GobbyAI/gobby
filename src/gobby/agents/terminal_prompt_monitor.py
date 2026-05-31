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


class TerminalPromptMonitor:
    """Detect and dismiss blocking prompts in spawned agent tmux panes."""

    def __init__(
        self,
        *,
        get_active_terminal_runs: Callable[[], list[AgentRun]],
        get_tmux: Callable[[], TmuxSessionManager],
        prompt_detector: PromptDetector,
        loop_tracker: LoopTracker,
        get_tmux_config: Callable[[], TmuxConfig],
        handle_looping_agent: Callable[[AgentRun], Awaitable[None]],
        monotonic: Callable[[], float] = time.monotonic,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._get_active_terminal_runs = get_active_terminal_runs
        self._get_tmux = get_tmux
        self._prompt_detector = prompt_detector
        self._loop_tracker = loop_tracker
        self._get_tmux_config = get_tmux_config
        self._handle_looping_agent = handle_looping_agent
        self._monotonic = monotonic
        self._last_enter_sent_at: dict[str, float] = {}
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

    async def check_trust_prompts(self) -> int:
        """Check for folder trust prompts and auto-dismiss them."""
        runs = await self._run_db(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            if self._prompt_detector.was_dismissed(run.id):
                continue

            tmux_name = run.tmux_session_name
            assert tmux_name is not None

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=15)
                if pane_output and self._prompt_detector.detect_trust_prompt(pane_output):
                    sent = await self._get_tmux().send_keys(
                        tmux_name,
                        PromptDetector.TRUST_DISMISS_KEYS,
                    )
                    if sent:
                        self.mark_enter_sent(run.id)
                        self._prompt_detector.mark_dismissed(run.id)
                        logger.info(
                            "Auto-dismissed trust prompt for agent %s (trust folder)",
                            run.id,
                        )
                        handled += 1
            except Exception as e:
                logger.warning("Error checking trust prompt for agent %s: %s", run.id, e)

        return handled

    async def check_loop_prompts(self) -> int:
        """Check for loop detection prompts and auto-dismiss them."""
        runs = await self._run_db(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            tmux_name = run.tmux_session_name
            assert tmux_name is not None

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=15)
                if pane_output and self._prompt_detector.detect_loop_prompt(pane_output):
                    count = self._loop_tracker.record_dismissal(run.id)

                    if self._loop_tracker.should_escalate(run.id):
                        logger.warning(
                            "Doom loop detected for agent %s: %s loop prompts dismissed, "
                            "escalating to kill",
                            run.id,
                            count,
                        )
                        await self._handle_looping_agent(run)
                    else:
                        sent = await self._get_tmux().send_keys(
                            tmux_name,
                            PromptDetector.LOOP_DISMISS_KEYS,
                        )
                        if sent:
                            self.mark_enter_sent(run.id)
                            logger.info(
                                "Auto-dismissed loop prompt for agent %s (%s/%s)",
                                run.id,
                                count,
                                self._loop_tracker.threshold,
                            )
                            handled += 1
            except Exception as e:
                logger.warning("Error checking loop prompt for agent %s: %s", run.id, e)

        return handled

    async def check_approval_prompts(self) -> int:
        """Check for approval prompts whose visible text says Enter approves."""
        if not self._get_tmux_config().auto_enter_approval_prompts:
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)

        handled = 0
        for run in runs:
            tmux_name = run.tmux_session_name
            assert tmux_name is not None

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=15)
                if not pane_output or not self._prompt_detector.detect_approval_prompt(pane_output):
                    continue
                if self._prompt_detector.was_approval_prompt_dismissed(run.id, pane_output):
                    continue

                sent = await self._get_tmux().send_keys(
                    tmux_name,
                    PromptDetector.ENTER_KEY,
                    literal=False,
                )
                if sent:
                    self.mark_enter_sent(run.id)
                    self._prompt_detector.mark_approval_prompt_dismissed(run.id, pane_output)
                    logger.info("Auto-entered approval prompt for agent %s", run.id)
                    handled += 1
            except Exception as e:
                logger.warning("Error checking approval prompt for agent %s: %s", run.id, e)

        return handled

    async def check_queued_continuation_prompts(self) -> int:
        """Observe queued Gobby continuation prompts without editing the CLI input queue."""
        if not self._get_tmux_config().auto_enter_agent_terminals:
            return 0

        runs = await self._run_db(self._get_active_terminal_runs)

        for run in runs:
            tmux_name = run.tmux_session_name
            assert tmux_name is not None

            try:
                pane_output = await self._get_tmux().capture_pane(tmux_name, lines=30)
                if not pane_output:
                    continue
                if not self._prompt_detector.detect_queued_continuation_prompt(pane_output):
                    continue

                # Observation-only: periodic enter handling owns any actual key submission.
                logger.info(
                    "Observed queued continuation prompt for agent %s; leaving input queue untouched",
                    run.id,
                )
            except Exception as e:
                logger.warning("Error checking queued continuation for agent %s: %s", run.id, e)

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
            tmux_name = run.tmux_session_name
            assert tmux_name is not None

            last_sent = self._last_enter_sent_at.get(run.id)
            if last_sent is not None and now - last_sent < interval:
                continue

            try:
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
                logger.warning("Error sending periodic Enter to agent %s: %s", run.id, e)

        return handled
