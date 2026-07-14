"""Circuit breaker for vector sync when the embedding endpoint is down.

Without this, an unreachable embedding endpoint produced a sustained storm of
per-file gcode subprocess spawns, failure UPDATEs, and ERROR logs (~10 files/s
indefinitely) — the dominant disk-churn driver in incident #18196. The breaker
trips only on transport-class failures; per-file data errors never open it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import Enum

logger = logging.getLogger(__name__)


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class VectorSyncBreaker:
    """CLOSED → (N consecutive transport failures) → OPEN → backoff →
    HALF_OPEN single-file probe → success closes / failure reopens with
    doubled backoff (capped). Logs exactly once per state transition."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        base_backoff_seconds: float = 30.0,
        max_backoff_seconds: float = 900.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._base_backoff = base_backoff_seconds
        self._max_backoff = max_backoff_seconds
        self._monotonic = monotonic
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._current_backoff = base_backoff_seconds
        self._open_until = 0.0

    @property
    def state(self) -> BreakerState:
        return self._state

    def pending_allowed(self) -> bool:
        """Whether vector-pending files should be fetched at all this pass.

        False while OPEN with unelapsed backoff and while a HALF_OPEN probe is
        outstanding — graph-only batches, zero vector churn.
        """
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN:
            return self._monotonic() >= self._open_until
        return False

    def should_attempt(self) -> bool:
        """Per-file gate for the vector branch.

        CLOSED: always. OPEN with elapsed backoff: transitions to HALF_OPEN and
        allows exactly one probe attempt. Otherwise: skip.
        """
        if self._state is BreakerState.CLOSED:
            return True
        if self._state is BreakerState.OPEN and self._monotonic() >= self._open_until:
            self._state = BreakerState.HALF_OPEN
            logger.info("Vector sync breaker half-open: probing embedding endpoint with one file")
            return True
        return False

    def record_success(self) -> None:
        if self._state is not BreakerState.CLOSED:
            logger.info("Vector sync breaker closed: embedding endpoint recovered")
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._current_backoff = self._base_backoff

    def record_failure(self) -> None:
        """Record a transport-class failure (timeout/unavailable/embedding transport)."""
        if self._state is BreakerState.HALF_OPEN:
            self._current_backoff = min(self._current_backoff * 2, self._max_backoff)
            self._open(probe_failed=True)
            return
        if self._state is BreakerState.OPEN:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._open(probe_failed=False)

    def _open(self, *, probe_failed: bool) -> None:
        self._state = BreakerState.OPEN
        self._open_until = self._monotonic() + self._current_backoff
        reason = (
            "probe failed"
            if probe_failed
            else (f"{self._consecutive_failures} consecutive transport failures")
        )
        logger.warning(
            "Vector sync breaker open (%s): pausing vector sync for %.0fs",
            reason,
            self._current_backoff,
        )
