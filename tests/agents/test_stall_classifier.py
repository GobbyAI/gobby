"""Tests for gobby.agents.stall_classifier module."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gobby.agents.stall_classifier import (
    _CONSECUTIVE_THRESHOLD,
    _MIN_CHECK_INTERVAL_SECONDS,
    StallClassifier,
    StallStatus,
)

pytestmark = pytest.mark.unit


class TestIsProviderError:
    """Stateless pattern matching tests."""

    @pytest.mark.parametrize(
        "error",
        [
            # HTTP status codes with context
            "429 rate limit exceeded",
            "429 Too Many Requests",
            "503 service unavailable",
            "502 bad gateway error",
            "500 internal server error",
            "API Error: 529 Overloaded. This is a server-side issue, usually temporary.",
            # Rate limiting with error context
            "Error: rate limited by provider",
            "rate limit exceeded, please retry",
            "failed: rate-limiting in effect",
            "Error: too many requests, please retry",
            "quota exceeded",
            "quota exhausted",
            # Timeout / connectivity
            "request timed out after 30s",
            "connection timed out",
            "read timeout",
            "ETIMEDOUT connecting to api.anthropic.com",
            "ECONNREFUSED",
            "ECONNRESET by peer",
            "network error during request",
            # Provider-specific error types
            "overloaded_error",
            "ResourceExhausted: quota",
            "capacity exceeded",
            # Exception class names
            "APIConnectionError: connection failed",
            "APIStatusError: 529",
            "InternalServerError from provider",
            "anthropic.APIError: rate limit",
            "anthropic.RateLimitError: too many requests",
            (
                "Provider bootstrap/accounting stall: terminal output was visible "
                "but Gobby session accounting stayed at zero"
            ),
        ],
    )
    def test_matches_provider_errors(self, error: str) -> None:
        classifier = StallClassifier()
        assert classifier.is_provider_error(error) is True

    def test_identifies_bootstrap_stall(self) -> None:
        classifier = StallClassifier()
        assert (
            classifier.is_bootstrap_stall(
                "Provider bootstrap/accounting stall: session accounting stayed at zero"
            )
            is True
        )
        assert classifier.is_bootstrap_stall("503 service unavailable") is False

    @pytest.mark.parametrize(
        "error",
        [
            "SyntaxError: unexpected token",
            "FileNotFoundError: /tmp/missing.py",
            "AssertionError: expected 3 got 4",
            "TypeError: cannot add str and int",
            "task failed validation",
            "git merge conflict in file.py",
            # Task content that should NOT trigger (the whole point of this fix)
            "Add rate limit handling to adapters",
            "implement rate_limit_backoff()",
            "rate_limiter = TokenBucketRateLimiter()",
            "tokens per minute configuration",
            "model busy flag check",
            "from anthropic import Anthropic",
            "",
            None,
        ],
    )
    def test_does_not_match_task_errors(self, error: str | None) -> None:
        classifier = StallClassifier()
        assert classifier.is_provider_error(error) is False


class TestClassify:
    """Stateful classification with consecutive-check tracking."""

    def test_healthy_on_empty_input(self) -> None:
        classifier = StallClassifier()
        result = classifier.classify("run-1")
        assert result.status == StallStatus.HEALTHY

    def test_healthy_on_normal_output(self) -> None:
        classifier = StallClassifier()
        result = classifier.classify("run-1", pane_output="Working on task...\nEditing file.py")
        assert result.status == StallStatus.HEALTHY

    def test_single_provider_error_returns_unknown(self) -> None:
        """First detection should be UNKNOWN (not yet confirmed)."""
        classifier = StallClassifier()
        result = classifier.classify("run-1", pane_output="Error: 429 rate limit exceeded")
        assert result.status == StallStatus.UNKNOWN
        assert result.consecutive_hits == 1

    def test_consecutive_errors_confirm_stall(self) -> None:
        """Two consecutive checks with provider errors = confirmed stall."""
        classifier = StallClassifier()

        # First check
        with patch("gobby.agents.stall_classifier.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            classifier.classify("run-1", pane_output="429 rate limit exceeded")

            # Second check after interval
            mock_time.monotonic.return_value = _MIN_CHECK_INTERVAL_SECONDS + 1
            result = classifier.classify("run-1", pane_output="429 rate limit exceeded")

        assert result.status == StallStatus.PROVIDER_STALL
        assert result.consecutive_hits == _CONSECUTIVE_THRESHOLD

    def test_healthy_output_resets_consecutive_count(self) -> None:
        """A healthy check between errors should reset the counter."""
        classifier = StallClassifier()

        with patch("gobby.agents.stall_classifier.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            classifier.classify("run-1", pane_output="429 rate limit exceeded")

            # Healthy output resets
            mock_time.monotonic.return_value = 35.0
            result = classifier.classify("run-1", pane_output="Editing file.py...")
            assert result.status == StallStatus.HEALTHY

            # Next error starts fresh at 1
            mock_time.monotonic.return_value = 70.0
            result = classifier.classify("run-1", pane_output="429 rate limit exceeded")
            assert result.status == StallStatus.UNKNOWN
            assert result.consecutive_hits == 1

    def test_rapid_checks_dont_increment(self) -> None:
        """Two checks within MIN_CHECK_INTERVAL shouldn't double-count."""
        classifier = StallClassifier()

        with patch("gobby.agents.stall_classifier.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            classifier.classify("run-1", pane_output="429 rate limit exceeded")

            # Too soon — should not increment
            mock_time.monotonic.return_value = 5.0
            result = classifier.classify("run-1", pane_output="429 rate limit exceeded")
            assert result.consecutive_hits == 1  # Still 1

    def test_error_field_also_matched(self) -> None:
        """Error string (from agent_runs.error) should be checked too."""
        classifier = StallClassifier()

        with patch("gobby.agents.stall_classifier.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            classifier.classify("run-1", error="overloaded_error")

            mock_time.monotonic.return_value = _MIN_CHECK_INTERVAL_SECONDS + 1
            result = classifier.classify("run-1", error="overloaded_error")

        assert result.status == StallStatus.PROVIDER_STALL

    def test_independent_tracking_per_run(self) -> None:
        """Each run_id has independent state."""
        classifier = StallClassifier()

        with patch("gobby.agents.stall_classifier.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            classifier.classify("run-1", pane_output="429 rate limit exceeded")
            classifier.classify("run-2", pane_output="Working fine")

            mock_time.monotonic.return_value = _MIN_CHECK_INTERVAL_SECONDS + 1
            r1 = classifier.classify("run-1", pane_output="429 rate limit exceeded")
            r2 = classifier.classify("run-2", pane_output="429 rate limit exceeded")

        assert r1.status == StallStatus.PROVIDER_STALL
        assert r2.status == StallStatus.UNKNOWN  # Only first hit for run-2

    def test_clear_removes_state(self) -> None:
        """Clearing state should reset tracking for a run."""
        classifier = StallClassifier()

        with patch("gobby.agents.stall_classifier.time") as mock_time:
            mock_time.monotonic.return_value = 0.0
            classifier.classify("run-1", pane_output="429 rate limit exceeded")

            classifier.clear("run-1")

            mock_time.monotonic.return_value = _MIN_CHECK_INTERVAL_SECONDS + 1
            result = classifier.classify("run-1", pane_output="429 rate limit exceeded")

        # Should be back to 1 (fresh state)
        assert result.consecutive_hits == 1
        assert result.status == StallStatus.UNKNOWN
