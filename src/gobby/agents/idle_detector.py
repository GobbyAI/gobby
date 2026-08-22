"""Idle agent classification via tmux pane content analysis.

Secondary idle detection layer: classifies *why* an agent is idle by
examining the last few lines of its tmux pane (prompt, context full, etc.).
The primary idle signal is session updated_at in lifecycle_monitor.py;
pane analysis only runs when the session appears stale.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from gobby.agents.detection.matcher import CompiledManifest
from gobby.agents.detection.provider import DetectionRegistry, resolve_manifest


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

    REPROMPT_MESSAGE = "Continue working on your task."

    def __init__(self, registry: DetectionRegistry, provider_id: str | None = None) -> None:
        self._registry = registry
        self._provider_id = provider_id.strip().lower() if provider_id is not None else None
        self._providers: dict[str, IdleDetector] = {}
        self._states: dict[str, IdleState] = {}

    def for_provider(self, provider_id: str) -> IdleDetector:
        """Return the cached detector bound to one provider."""

        normalized = provider_id.strip().lower()
        if self._provider_id == normalized:
            return self
        cached = self._providers.get(normalized)
        if cached is None:
            cached = IdleDetector(self._registry, normalized)
            cached._states = self._states
            self._providers[normalized] = cached
        return cached

    def _manifest(self) -> CompiledManifest | None:
        if self._provider_id is None:
            raise RuntimeError("IdleDetector must be bound with for_provider() before detection")
        return resolve_manifest(self._registry, self._provider_id)

    def get_state(self, run_id: str) -> IdleState:
        """Get or create idle state for an agent."""
        if run_id not in self._states:
            self._states[run_id] = IdleState()
        return self._states[run_id]

    def clear_state(self, run_id: str) -> None:
        """Remove tracking state for an agent (on cleanup)."""
        for detector in self._providers.values():
            detector.clear_state(run_id)
        self._states.pop(run_id, None)

    def unsubmitted_input_fingerprint(self, pane_output: str) -> str | None:
        """Fingerprint normalized draft lines typed at a provider prompt."""
        manifest = self._manifest()
        if manifest is None:
            return None
        if manifest.match_rule("queued_continuation", pane_output).match is not None:
            return None
        if manifest.match_rule("queued_message", pane_output).match is not None:
            return None

        draft_lines: list[str] = []
        for line in pane_output.splitlines():
            stripped = line.strip()
            if manifest.match_rule("stalled_input", stripped).match is not None:
                draft_lines.append(" ".join(stripped.split()))
        if not draft_lines:
            return None
        return hashlib.sha256("\n".join(draft_lines).encode()).hexdigest()

    def has_unsubmitted_input(self, pane_output: str) -> bool:
        """Return whether pane output shows text typed at a prompt but not submitted."""
        return self.unsubmitted_input_fingerprint(pane_output) is not None

    def detect(self, pane_output: str) -> str:
        """Classify pane output as 'idle', 'context_full', or 'active'.

        Args:
            pane_output: Last few lines captured from the tmux pane.

        Returns:
            One of: 'idle', 'context_full', 'active'
        """
        manifest = self._manifest()
        if manifest is None:
            return "unknown"

        lines = pane_output.strip().splitlines()
        if not lines:
            return "active"

        if manifest.match_rule("context_full", pane_output).match is not None:
            return "context_full"
        if manifest.match_rule("stop_hook_blocked", pane_output).match is not None:
            return "idle"
        has_queued = manifest.match_rule("queued_message", pane_output).match is not None
        has_active = manifest.match_rule("active_work", pane_output).match is not None
        if has_queued and has_active:
            return "active"
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if manifest.match_rule("status_bar", stripped).match is not None:
                continue
            if manifest.match_rule("idle_prompt", stripped).match is not None:
                return "idle"
            if manifest.match_rule("stalled_input", stripped).match is not None:
                return "idle"
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
        state.reprompt_count = 0
        state.last_reprompt_at = None
