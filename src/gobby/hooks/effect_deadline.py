"""Shared deadline helpers for blocking hook effects."""

import time

BLOCKING_EFFECT_BUDGET_SECONDS = 20.0


def new_blocking_effect_deadline() -> float:
    """Return the monotonic deadline for one hook's blocking effects."""
    return time.monotonic() + BLOCKING_EFFECT_BUDGET_SECONDS


def remaining_blocking_effect_seconds(
    deadline: float | None,
    *,
    maximum: float,
) -> float:
    """Return the remaining bounded timeout for a blocking effect."""
    if deadline is None:
        return maximum
    return max(0.0, min(maximum, deadline - time.monotonic()))
