"""Provider stall detection for running agents.

Classifies agent failures as provider-side (rate limits, outages, timeouts)
vs task-side (bugs, logic errors). Used by the lifecycle monitor to decide
whether to retry with a different provider or re-dispatch normally.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum


class StallStatus(Enum):
    """Classification of agent health."""

    HEALTHY = "healthy"
    PROVIDER_STALL = "provider_stall"
    TASK_SLOW = "task_slow"
    UNKNOWN = "unknown"


@dataclass
class StallClassification:
    """Result of a stall check."""

    status: StallStatus
    reason: str | None = None
    consecutive_hits: int = 0


@dataclass
class _RunState:
    """Internal tracking state for a single agent run."""

    consecutive_provider_hits: int = 0
    last_check_at: float = 0.0
    last_status: StallStatus = StallStatus.HEALTHY


# Patterns that indicate provider-side errors (not the agent's fault).
#
# IMPORTANT: These patterns are matched against tmux pane output, which
# includes the agent's own working text (code, task descriptions, file
# contents). Patterns must be specific enough to avoid matching agents
# working on code *about* rate limiting, error handling, etc.
_PROVIDER_ERROR_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Gobby bootstrap/accounting failures: the terminal launched and may show
    # model output, but hooks/transcript accounting never attached to the run.
    re.compile(r"bootstrap/accounting stall", re.IGNORECASE),
    re.compile(r"session accounting stayed at zero", re.IGNORECASE),
    # HTTP status codes with error context — specific enough as-is
    re.compile(r"\b429\b.*(?:rate|limit|too many|quota)", re.IGNORECASE),
    re.compile(r"\b503\b.*(?:service|unavailable|overloaded)", re.IGNORECASE),
    re.compile(r"\b502\b.*(?:bad gateway|upstream)", re.IGNORECASE),
    re.compile(r"\b500\b.*(?:internal server error)", re.IGNORECASE),
    re.compile(r"\b529\b.*(?:overloaded|server-side|server side)", re.IGNORECASE),
    # Rate limiting — require error/exception context to avoid matching
    # code that discusses rate limiting (task titles, variable names, etc.)
    re.compile(r"(?:error|failed|exception|raise|fatal|❌).*rate.?limit", re.IGNORECASE),
    re.compile(r"rate.?limit.*(?:error|exception|exceeded|please retry)", re.IGNORECASE),
    re.compile(r"(?:error|failed|warning).*too many requests", re.IGNORECASE),
    re.compile(r"quota\s+(?:exceeded|exhausted)", re.IGNORECASE),
    # Timeout / connectivity — already specific
    re.compile(r"(?:request|connection|read)\s+timed?\s*out", re.IGNORECASE),
    re.compile(r"ETIMEDOUT|ECONNREFUSED|ECONNRESET", re.IGNORECASE),
    re.compile(r"network\s+error", re.IGNORECASE),
    # Provider-specific error types — already specific (class names / error codes)
    re.compile(r"overloaded_error", re.IGNORECASE),
    re.compile(r"ResourceExhausted", re.IGNORECASE),
    re.compile(r"capacity\s+exceeded", re.IGNORECASE),
    # Anthropic/OpenAI/Google — use specific exception class names
    re.compile(r"APIConnectionError", re.IGNORECASE),
    re.compile(r"APIStatusError", re.IGNORECASE),
    re.compile(r"InternalServerError", re.IGNORECASE),
    re.compile(r"anthropic\..*Error", re.IGNORECASE),
)

_BOOTSTRAP_STALL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"bootstrap/accounting stall", re.IGNORECASE),
    re.compile(r"session accounting stayed at zero", re.IGNORECASE),
)

# Minimum consecutive checks with provider errors before confirming stall
_CONSECUTIVE_THRESHOLD = 2

# Minimum seconds between checks that should elapse (prevents false positives
# from two rapid checks seeing the same error)
_MIN_CHECK_INTERVAL_SECONDS = 30.0


class StallClassifier:
    """Classifies whether an agent is stalled due to provider issues.

    Tracks consecutive provider error detections per agent run. A stall is
    confirmed after 2+ consecutive checks (at 30s intervals) show provider
    errors, preventing transient errors from triggering false positives.
    """

    def __init__(self) -> None:
        self._states: dict[str, _RunState] = {}

    def classify(
        self,
        run_id: str,
        pane_output: str | None = None,
        error: str | None = None,
    ) -> StallClassification:
        """Classify the current state of an agent run.

        Args:
            run_id: Agent run ID.
            pane_output: Recent tmux pane output (optional).
            error: Error string from agent_runs.error (optional).

        Returns:
            Classification with status, reason, and consecutive hit count.
        """
        state = self._states.setdefault(run_id, _RunState())
        now = time.monotonic()

        # Combine sources for pattern matching
        text = ""
        if pane_output:
            text += pane_output
        if error:
            text += "\n" + error

        if not text.strip():
            state.consecutive_provider_hits = 0
            state.last_status = StallStatus.HEALTHY
            state.last_check_at = now
            return StallClassification(status=StallStatus.HEALTHY)

        # Check for provider error patterns
        matched_reason = self._match_provider_error(text)

        if matched_reason:
            # First hit always counts; subsequent hits require enough elapsed time
            # to prevent rapid re-checks from double-counting the same error
            if state.consecutive_provider_hits == 0:
                state.consecutive_provider_hits = 1
            else:
                elapsed = now - state.last_check_at
                if elapsed >= _MIN_CHECK_INTERVAL_SECONDS:
                    state.consecutive_provider_hits += 1

            state.last_check_at = now

            if state.consecutive_provider_hits >= _CONSECUTIVE_THRESHOLD:
                state.last_status = StallStatus.PROVIDER_STALL
                return StallClassification(
                    status=StallStatus.PROVIDER_STALL,
                    reason=matched_reason,
                    consecutive_hits=state.consecutive_provider_hits,
                )
            else:
                # Not enough consecutive hits yet
                state.last_status = StallStatus.UNKNOWN
                return StallClassification(
                    status=StallStatus.UNKNOWN,
                    reason=f"possible provider issue: {matched_reason}",
                    consecutive_hits=state.consecutive_provider_hits,
                )
        else:
            # No provider error — reset consecutive count
            state.consecutive_provider_hits = 0
            state.last_check_at = now
            state.last_status = StallStatus.HEALTHY
            return StallClassification(status=StallStatus.HEALTHY)

    def is_provider_error(self, error_string: str | None) -> bool:
        """Check if an error string matches provider error patterns.

        Stateless convenience method for post-mortem classification
        (e.g., checking agent_runs.error after an agent dies).

        Args:
            error_string: Error message to check.

        Returns:
            True if the error matches a known provider error pattern.
        """
        if not error_string:
            return False
        return self._match_provider_error(error_string) is not None

    def is_bootstrap_stall(self, error_string: str | None) -> bool:
        """Check if an error string is Gobby bootstrap/accounting containment."""
        if not error_string:
            return False
        return any(pattern.search(error_string) for pattern in _BOOTSTRAP_STALL_PATTERNS)

    def clear(self, run_id: str) -> None:
        """Remove tracking state for an agent run."""
        self._states.pop(run_id, None)

    @staticmethod
    def _match_provider_error(text: str) -> str | None:
        """Return the first matching provider error reason, or None."""
        for pattern in _PROVIDER_ERROR_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)
        return None
