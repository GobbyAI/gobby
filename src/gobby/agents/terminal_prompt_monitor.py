"""Terminal prompt checks for tmux-backed agent runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

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
    ) -> None:
        self._get_active_terminal_runs = get_active_terminal_runs
        self._get_tmux = get_tmux
        self._prompt_detector = prompt_detector
        self._loop_tracker = loop_tracker
        self._get_tmux_config = get_tmux_config
        self._handle_looping_agent = handle_looping_agent

    async def check_trust_prompts(self) -> int:
        """Check for folder trust prompts and auto-dismiss them."""
        runs = await asyncio.to_thread(self._get_active_terminal_runs)

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
        runs = await asyncio.to_thread(self._get_active_terminal_runs)

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

        runs = await asyncio.to_thread(self._get_active_terminal_runs)

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
                    PromptDetector.APPROVAL_DISMISS_KEYS,
                )
                if sent:
                    self._prompt_detector.mark_approval_prompt_dismissed(run.id, pane_output)
                    logger.info("Auto-entered approval prompt for agent %s", run.id)
                    handled += 1
            except Exception as e:
                logger.warning("Error checking approval prompt for agent %s: %s", run.id, e)

        return handled
