"""Loop prompt dismissal tracker for doom loop detection.

Tracks how many times each agent run has had its loop detection prompt
auto-dismissed. When the count exceeds a threshold, the lifecycle monitor
should escalate (checkpoint + kill) instead of dismissing again.

Persist counts in run metadata when a run manager is supplied, so terminal
hosts retaining agents across daemon restarts cannot reset escalation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.storage.agents import LocalAgentRunManager


_COUNT_KEY = "loop_dismissal_count"


class LoopTracker:
    """Tracks loop prompt dismissals per agent run for escalation."""

    def __init__(
        self, threshold: int = 3, *, run_manager: LocalAgentRunManager | None = None
    ) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._counts: dict[str, int] = {}
        self._threshold = threshold
        self._run_manager = run_manager

    @property
    def threshold(self) -> int:
        """The escalation threshold."""
        return self._threshold

    def record_dismissal(self, run_id: str) -> int:
        """Record a loop prompt dismissal. Returns the new count."""
        if self._run_manager is not None:
            return self._run_manager.increment_loop_dismissal_count(run_id)
        self._counts[run_id] = self._counts.get(run_id, 0) + 1
        return self._counts[run_id]

    def should_escalate(self, run_id: str) -> bool:
        """True if dismissals >= threshold for this run."""
        return self.get_count(run_id) >= self._threshold

    def get_count(self, run_id: str) -> int:
        """Current dismissal count for a run."""
        if self._run_manager is not None:
            run = self._run_manager.get(run_id)
            count = (run.resume_metadata_json or {}).get(_COUNT_KEY, 0) if run else 0
            return count if isinstance(count, int) and not isinstance(count, bool) else 0
        return self._counts.get(run_id, 0)

    def clear(self, run_id: str) -> None:
        """Remove tracking state on cleanup."""
        if self._run_manager is not None:
            self._run_manager.merge_resume_metadata(run_id, {_COUNT_KEY: 0})
        self._counts.pop(run_id, None)
