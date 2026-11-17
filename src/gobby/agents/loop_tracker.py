"""In-memory loop prompt dismissal tracker for doom loop detection.

Tracks how many times each agent run has had its loop detection prompt
auto-dismissed. When the count exceeds a threshold, the lifecycle monitor
should escalate (checkpoint + kill) instead of dismissing again.

Pure in-memory — dismissal counts reset when the daemon restarts.
Terminal hosts retain running agents independently of the daemon.
"""

from __future__ import annotations


class LoopTracker:
    """Tracks loop prompt dismissals per agent run for escalation."""

    def __init__(self, threshold: int = 3) -> None:
        if threshold < 1:
            raise ValueError(f"threshold must be >= 1, got {threshold}")
        self._counts: dict[str, int] = {}
        self._threshold = threshold

    @property
    def threshold(self) -> int:
        """The escalation threshold."""
        return self._threshold

    def record_dismissal(self, run_id: str) -> int:
        """Record a loop prompt dismissal. Returns the new count."""
        self._counts[run_id] = self._counts.get(run_id, 0) + 1
        return self._counts[run_id]

    def should_escalate(self, run_id: str) -> bool:
        """True if dismissals >= threshold for this run."""
        return self._counts.get(run_id, 0) >= self._threshold

    def get_count(self, run_id: str) -> int:
        """Current dismissal count for a run."""
        return self._counts.get(run_id, 0)

    def clear(self, run_id: str) -> None:
        """Remove tracking state on cleanup."""
        self._counts.pop(run_id, None)
