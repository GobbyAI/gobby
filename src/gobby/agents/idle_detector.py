"""Idle agent classification via tmux pane content analysis.

Secondary idle detection layer: classifies *why* an agent is idle by
examining the last few lines of its tmux pane (prompt, context full, etc.).
The primary idle signal is session updated_at in lifecycle_monitor.py;
pane analysis only runs when the session appears stale.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass


@dataclass
class IdleState:
    """Tracks idle state for a single agent."""

    first_idle_at: float | None = None
    reprompt_count: int = 0
    last_reprompt_at: float | None = None


class IdleDetector:
    """Detects idle agents by pattern-matching tmux pane output.

    Three detection modes:
    1. **Idle prompt** — agent is sitting at ❯ or $ prompt (repromptable)
    2. **Context full** — agent hit context limits (immediate fail, reprompt won't help)
    3. **Active** — agent is still working (no action needed)
    """

    IDLE_PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"^\s*[❯>]\s*$"),  # Claude Code idle prompt
        re.compile(r"^\s*\$\s*$"),  # Shell prompt (agent exited)
    )

    # Patterns that indicate text is sitting in the tmux buffer unsubmitted.
    # The agent typed something but never hit Enter — treated as idle so the
    # lifecycle monitor can submit it via send_keys("\n").
    STALLED_BUFFER_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"^\s*[❯>]\s+\S"),  # Prompt char followed by unsubmitted text
        re.compile(r"^\s*\$\s+\S"),  # Shell prompt with trailing text
    )

    # Patterns that indicate agent tried to stop but was blocked by a hook.
    # Treated as idle — the agent isn't doing useful work, it's stuck.
    STOP_HOOK_BLOCKED_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"Stop hook error", re.IGNORECASE),
        re.compile(r"Rule enforced by Gobby", re.IGNORECASE),
    )

    QUEUED_MESSAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"queued messages", re.IGNORECASE),
        re.compile(r"Press up to edit queued messages", re.IGNORECASE),
    )

    ACTIVE_WORK_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"\bRunning\b", re.IGNORECASE),
        re.compile(r"almost done thinking", re.IGNORECASE),
        re.compile(r"thinking with .*effort", re.IGNORECASE),
        re.compile(r"\bReading\s+\d+\s+files?\b", re.IGNORECASE),
    )

    CONTEXT_FULL_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"context.*window.*full", re.IGNORECASE),
        re.compile(r"would you like to continue", re.IGNORECASE),
        re.compile(r"run out of context", re.IGNORECASE),
        re.compile(r"conversation is too long", re.IGNORECASE),
    )

    # Claude Code status bar lines — skip these when searching for the prompt
    STATUS_BAR_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"Opus|Sonnet|Haiku", re.IGNORECASE),
        re.compile(r"bypass permissions", re.IGNORECASE),
        re.compile(r"^\s*[⎇𖠰]"),  # Branch/worktree indicators
        re.compile(r"^\s*/"),  # Absolute path (cwd line)
        re.compile(r"^\s*[─━▪▫]+"),  # Separator/divider lines
    )

    REPROMPT_MESSAGE = "Continue working on your task."

    def __init__(self) -> None:
        self._states: dict[str, IdleState] = {}

    def get_state(self, run_id: str) -> IdleState:
        """Get or create idle state for an agent."""
        if run_id not in self._states:
            self._states[run_id] = IdleState()
        return self._states[run_id]

    def clear_state(self, run_id: str) -> None:
        """Remove tracking state for an agent (on cleanup)."""
        self._states.pop(run_id, None)

    def detect(self, pane_output: str) -> str:
        """Classify pane output as 'idle', 'context_full', or 'active'.

        Args:
            pane_output: Last few lines captured from the tmux pane.

        Returns:
            One of: 'idle', 'context_full', 'active'
        """
        lines = pane_output.strip().splitlines()
        if not lines:
            return "active"

        # Check all lines for context-full patterns (takes priority)
        full_text = "\n".join(lines)
        for pattern in self.CONTEXT_FULL_PATTERNS:
            if pattern.search(full_text):
                return "context_full"

        # Check all lines for stop-hook-blocked patterns (agent tried to exit)
        for pattern in self.STOP_HOOK_BLOCKED_PATTERNS:
            if pattern.search(full_text):
                return "idle"

        has_queued_message = any(
            pattern.search(full_text) for pattern in self.QUEUED_MESSAGE_PATTERNS
        )
        has_active_work = any(pattern.search(full_text) for pattern in self.ACTIVE_WORK_PATTERNS)
        if has_queued_message and has_active_work:
            return "active"

        # Check lines bottom-up, skipping status bar chrome
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip Claude Code status bar lines
            if any(p.search(stripped) for p in self.STATUS_BAR_PATTERNS):
                continue
            for pattern in self.IDLE_PROMPT_PATTERNS:
                if pattern.match(stripped):
                    return "idle"
            for pattern in self.STALLED_BUFFER_PATTERNS:
                if pattern.match(stripped):
                    return "idle"
            # First non-empty, non-status-bar line doesn't match idle → active
            break

        return "active"

    def should_reprompt(
        self,
        run_id: str,
        idle_timeout_seconds: int,
        max_reprompt_attempts: int,
    ) -> bool:
        """Check if an idle agent should be reprompted.

        Returns True if the agent has been idle long enough and hasn't
        exceeded max reprompt attempts.
        """
        state = self.get_state(run_id)
        now = time.monotonic()

        if state.first_idle_at is None:
            state.first_idle_at = now
            return False

        elapsed = now - state.first_idle_at
        if elapsed < idle_timeout_seconds:
            return False

        if state.reprompt_count >= max_reprompt_attempts:
            return False

        return True

    def should_fail(self, run_id: str, max_reprompt_attempts: int) -> bool:
        """Check if an idle agent should be failed (exhausted reprompts)."""
        state = self.get_state(run_id)
        return state.reprompt_count >= max_reprompt_attempts

    def record_reprompt(self, run_id: str) -> None:
        """Record that a reprompt was sent."""
        state = self.get_state(run_id)
        state.reprompt_count += 1
        state.last_reprompt_at = time.monotonic()
        # Reset idle timer so we wait again before next reprompt
        state.first_idle_at = time.monotonic()

    def reset_idle(self, run_id: str) -> None:
        """Reset idle tracking when agent becomes active again."""
        state = self.get_state(run_id)
        state.first_idle_at = None
