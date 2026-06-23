"""Tests for gobby.agents.provider_rotation module."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.agents.provider_rotation import (
    get_failed_providers_for_task,
    parse_provider_list,
    select_next_provider,
)

pytestmark = pytest.mark.unit


class TestParseProviderList:
    def test_single_provider(self) -> None:
        assert parse_provider_list("claude") == ["claude"]

    def test_multiple_providers(self) -> None:
        with pytest.warns(DeprecationWarning, match="Comma-separated"):
            assert parse_provider_list("qwen,claude") == ["qwen", "claude"]

    def test_whitespace_handling(self) -> None:
        with pytest.warns(DeprecationWarning, match="Comma-separated"):
            assert parse_provider_list("qwen , claude , codex") == ["qwen", "claude", "codex"]

    def test_none_returns_empty(self) -> None:
        assert parse_provider_list(None) == []

    def test_empty_string_returns_empty(self) -> None:
        assert parse_provider_list("") == []

    def test_case_normalization(self) -> None:
        with pytest.warns(DeprecationWarning, match="Comma-separated"):
            assert parse_provider_list("Claude,QWEN") == ["claude", "qwen"]


class TestGetFailedProviders:
    def test_returns_providers_with_provider_errors(self) -> None:
        mock_arm = MagicMock()
        mock_arm.db.fetchall.return_value = [
            {"provider": "qwen", "error": "429 rate limit exceeded"},
            {"provider": "claude", "error": "SyntaxError in code"},
        ]
        result = get_failed_providers_for_task("task-1", mock_arm)
        assert result == ["qwen"]

    def test_deduplicates_providers(self) -> None:
        mock_arm = MagicMock()
        mock_arm.db.fetchall.return_value = [
            {"provider": "qwen", "error": "429 rate limit exceeded"},
            {"provider": "qwen", "error": "503 service unavailable"},
        ]
        result = get_failed_providers_for_task("task-1", mock_arm)
        assert result == ["qwen"]

    def test_empty_when_no_provider_errors(self) -> None:
        mock_arm = MagicMock()
        mock_arm.db.fetchall.return_value = [
            {"provider": "claude", "error": "AssertionError: test failed"},
        ]
        result = get_failed_providers_for_task("task-1", mock_arm)
        assert result == []

    def test_empty_when_no_runs(self) -> None:
        mock_arm = MagicMock()
        mock_arm.db.fetchall.return_value = []
        result = get_failed_providers_for_task("task-1", mock_arm)
        assert result == []


class TestSelectNextProvider:
    def test_returns_none_when_not_provider_error(self) -> None:
        result = select_next_provider(
            "task-1",
            ["qwen", "claude"],
            failed_provider="qwen",
            is_provider_error=False,
        )
        assert result is None

    def test_returns_none_when_empty_list(self) -> None:
        result = select_next_provider(
            "task-1",
            [],
            failed_provider="qwen",
            is_provider_error=True,
        )
        assert result is None

    def test_skips_failed_provider(self) -> None:
        result = select_next_provider(
            "task-1",
            ["qwen", "claude"],
            failed_provider="qwen",
            is_provider_error=True,
        )
        assert result == "claude"

    def test_returns_none_when_all_exhausted(self) -> None:
        mock_arm = MagicMock()
        mock_arm.db.fetchall.return_value = [
            {"provider": "claude", "error": "429 rate limit exceeded"},
        ]
        result = select_next_provider(
            "task-1",
            ["qwen", "claude"],
            failed_provider="qwen",
            is_provider_error=True,
            agent_run_manager=mock_arm,
        )
        assert result is None

    def test_skips_historically_failed_providers(self) -> None:
        mock_arm = MagicMock()
        mock_arm.db.fetchall.return_value = [
            {"provider": "qwen", "error": "429 rate limit exceeded"},
        ]
        result = select_next_provider(
            "task-1",
            ["qwen", "claude", "codex"],
            failed_provider="qwen",
            is_provider_error=True,
            agent_run_manager=mock_arm,
        )
        assert result == "claude"

    def test_respects_provider_order(self) -> None:
        """First untried provider in the list wins."""
        result = select_next_provider(
            "task-1",
            ["claude", "qwen", "codex"],
            failed_provider="claude",
            is_provider_error=True,
        )
        assert result == "qwen"

    def test_no_agent_run_manager_uses_only_current_failure(self) -> None:
        result = select_next_provider(
            "task-1",
            ["qwen", "claude"],
            failed_provider="qwen",
            is_provider_error=True,
            agent_run_manager=None,
        )
        assert result == "claude"
