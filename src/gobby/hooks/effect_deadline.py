"""Shared deadline helpers for blocking hook effects."""

import time
from dataclasses import dataclass

BLOCKING_EFFECT_BUDGET_SECONDS = 20.0


@dataclass
class BlockingEffectDeadline:
    """Mutable monotonic deadline shared by one hook's blocking effects."""

    expires_at: float

    def extend(self, duration_seconds: float) -> None:
        """Exclude elapsed non-effect time from the shared blocking budget."""
        self.expires_at += duration_seconds


def new_blocking_effect_deadline() -> BlockingEffectDeadline:
    """Return the monotonic deadline for one hook's blocking effects."""
    return BlockingEffectDeadline(time.monotonic() + BLOCKING_EFFECT_BUDGET_SECONDS)


def remaining_blocking_effect_seconds(
    deadline: BlockingEffectDeadline | None,
    *,
    maximum: float,
) -> float:
    """Return the remaining bounded timeout for a blocking effect."""
    if deadline is None:
        return maximum
    return max(0.0, min(maximum, deadline.expires_at - time.monotonic()))


def elapsed_blocking_effect_seconds(deadline: BlockingEffectDeadline | None) -> float:
    """Return how much of the shared blocking budget this hook event has spent.

    Time excluded by :meth:`BlockingEffectDeadline.extend` does not count, so the
    result measures blocking-effect time rather than wall time since the event
    began. It exceeds ``BLOCKING_EFFECT_BUDGET_SECONDS`` once the budget is gone,
    and that overrun is what makes an exhausted deadline diagnosable.
    """
    if deadline is None:
        return 0.0
    return BLOCKING_EFFECT_BUDGET_SECONDS - (deadline.expires_at - time.monotonic())
