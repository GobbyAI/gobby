"""Tests for resolve_context_window in gobby.llm.claude_models.

Verifies the priority order:
1. Config overrides (model substring -> context window)
2. Registry lookup (OpenRouter data via cost_table cache)
3. Claude default fallback (200K)

Note: SDK-reported contextWindow (2nd arg) is deprecated and ignored.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from gobby.llm.claude_models import (
    CLAUDE_DEFAULT_CONTEXT_WINDOW,
    resolve_context_window,
)

pytestmark = pytest.mark.unit


def _mock_lookup(model: str) -> int | None:
    """Mock cost_table.lookup_context_window with test data."""
    data = {
        "claude-opus-4-6": 1_000_000,
        "claude-sonnet-4-6": 200_000,
        "claude-haiku-4-5": 200_000,
        "gpt-4o": 128_000,
    }
    # Strip provider prefix
    if "/" in model:
        model = model.split("/", 1)[1]
    # Exact match
    if model in data:
        return data[model]
    # Prefix match
    best_len = 0
    best_val = None
    for key, val in data.items():
        if model.startswith(key) and len(key) > best_len:
            best_len = len(key)
            best_val = val
    return best_val


class TestResolveContextWindow:
    """Tests for resolve_context_window()."""

    def test_sdk_context_window_ignored(self) -> None:
        """SDK-reported contextWindow (2nd arg) is deprecated and ignored."""
        model_usage = {"contextWindow": 180_000}
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("claude-opus-4-6", model_usage)
        assert result == 1_000_000

    def test_claude_model_family_windows(self) -> None:
        """Claude models return registry-backed context windows."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            assert resolve_context_window("claude-opus-4-6", None) == 1_000_000
            assert resolve_context_window("claude-sonnet-4-6", None) == 200_000
            assert resolve_context_window("claude-haiku-4-5", None) == 200_000

    def test_claude_name_variations(self) -> None:
        """Various Claude model name formats resolve correctly."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            # Prefix match: claude-sonnet-4-6-20241022 matches claude-sonnet-4-6
            assert resolve_context_window("claude-sonnet-4-6-20241022", None) == 200_000

    def test_non_claude_model_uses_registry(self) -> None:
        """Non-Claude models use registry lookup."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("gpt-4o", None)
        assert result == 128_000

    def test_registry_miss_non_claude_returns_none(self) -> None:
        """If registry has no data for a non-Claude model, return None."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            result = resolve_context_window("unknown-model-xyz", None)
        assert result is None

    def test_registry_miss_claude_returns_default(self) -> None:
        """If registry has no data for a Claude model, return CLAUDE_DEFAULT."""
        with patch("gobby.llm.model_registry.lookup_context_window", return_value=None):
            result = resolve_context_window("claude-unknown-model", None)
        assert result == CLAUDE_DEFAULT_CONTEXT_WINDOW

    def test_none_model_returns_none(self) -> None:
        """None model returns None."""
        assert resolve_context_window(None, None) is None

    def test_none_model_with_sdk_usage_ignored(self) -> None:
        """None model -- SDK usage (2nd arg) is ignored, returns None."""
        model_usage = {"contextWindow": 200_000}
        result = resolve_context_window(None, model_usage)
        assert result is None

    def test_config_overrides_win_over_registry(self) -> None:
        """Config overrides take precedence over registry data."""
        overrides = {"opus": 500_000}
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("claude-opus-4-6", None, overrides=overrides)
        assert result == 500_000

    def test_config_overrides_partial(self) -> None:
        """Config overrides only affect matched families, others use registry."""
        overrides = {"opus": 500_000}
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            assert resolve_context_window("claude-opus-4-6", None, overrides=overrides) == 500_000
            assert resolve_context_window("claude-sonnet-4-6", None, overrides=overrides) == 200_000

    def test_overrides_win_sdk_ignored(self) -> None:
        """Config overrides win; SDK-reported contextWindow (2nd arg) is ignored."""
        overrides = {"opus": 500_000}
        model_usage = {"contextWindow": 180_000}
        result = resolve_context_window("claude-opus-4-6", model_usage, overrides=overrides)
        assert result == 500_000

    def test_provider_prefix_handled(self) -> None:
        """Provider-prefixed model names work via registry lookup."""
        with patch("gobby.llm.model_registry.lookup_context_window", side_effect=_mock_lookup):
            result = resolve_context_window("anthropic/claude-sonnet-4-6", None)
        assert result == 200_000
