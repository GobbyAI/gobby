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

from gobby.agents.detection.matcher import CompiledManifest
from gobby.agents.detection.provider import DetectionRegistry, resolve_manifest


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


_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

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

    def __init__(self, registry: DetectionRegistry, provider_id: str | None = None) -> None:
        self._registry = registry
        self._provider_id = provider_id.strip().lower() if provider_id is not None else None
        self._providers: dict[str, StallClassifier] = {}
        self._states: dict[str, _RunState] = {}

    def for_provider(self, provider_id: str) -> StallClassifier:
        """Return the cached classifier bound to one provider."""

        normalized = provider_id.strip().lower()
        if self._provider_id == normalized:
            return self
        cached = self._providers.get(normalized)
        if cached is None:
            cached = StallClassifier(self._registry, normalized)
            cached._states = self._states
            self._providers[normalized] = cached
        return cached

    def _manifest(self) -> CompiledManifest | None:
        if self._provider_id is None:
            raise RuntimeError("StallClassifier must be bound with for_provider() before detection")
        return resolve_manifest(self._registry, self._provider_id)

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

        if self._manifest() is None:
            state.consecutive_provider_hits = 0
            state.last_status = StallStatus.UNKNOWN
            state.last_check_at = now
            return StallClassification(status=StallStatus.UNKNOWN)

        has_pane_output = bool(pane_output and pane_output.strip())
        has_error = bool(error and error.strip())
        if not has_pane_output and not has_error:
            state.consecutive_provider_hits = 0
            state.last_status = StallStatus.HEALTHY
            state.last_check_at = now
            return StallClassification(status=StallStatus.HEALTHY)

        matched_reason = self._match_provider_error(error or "")
        if matched_reason is None:
            matched_reason = self._match_pane_provider_error(pane_output or "")

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
        for classifier in self._providers.values():
            classifier.clear(run_id)
        self._states.pop(run_id, None)

    def _match_provider_error(self, text: str) -> str | None:
        """Return the first matching provider error reason, or None."""
        manifest = self._manifest()
        if manifest is None:
            return None
        match = manifest.match_rule("provider_error", text).match
        return text if match is not None else None

    def _match_pane_provider_error(self, pane_output: str) -> str | None:
        """Return provider error evidence from live pane output, or None."""
        manifest = self._manifest()
        if manifest is None:
            return None
        for raw_line in pane_output.splitlines():
            line = self._normalize_pane_line(raw_line)
            if not line:
                continue
            if manifest.match_rule("source_shaped", line).match is not None:
                continue
            match = manifest.match_rule("pane_provider_error", line).match
            if match is not None:
                return line
        return None

    @staticmethod
    def _normalize_pane_line(line: str) -> str:
        """Strip terminal controls before matching visible pane text."""
        without_ansi = _ANSI_ESCAPE_RE.sub("", line)
        return _CONTROL_CHARS_RE.sub("", without_ansi).strip()
