"""Tests for resolve_context_window in gobby.llm.claude_models.

Verifies the priority order:
1. SDK-reported contextWindow from _model_usage (authoritative)
2. Static 200K for Claude models (never trust litellm for these)
3. litellm lookup for non-Claude models only
"""

from unittest.mock import MagicMock, patch

import pytest

from gobby.llm.claude_models import CLAUDE_DEFAULT_CONTEXT_WINDOW, resolve_context_window

pytestmark = pytest.mark.unit


class TestResolveContextWindow:
    """Tests for resolve_context_window()."""

    def test_sdk_context_window_takes_priority(self) -> None:
        """SDK-reported contextWindow should be used first, even for Claude models."""
        model_usage = {"contextWindow": 180_000}
        result = resolve_context_window("claude-opus-4-6", model_usage)
        assert result == 180_000

    def test_sdk_1m_context_window_trusted(self) -> None:
        """If the SDK reports 1M context window, trust it (user may have the beta)."""
        model_usage = {"contextWindow": 1_000_000}
        result = resolve_context_window("claude-opus-4-6", model_usage)
        assert result == 1_000_000

    def test_claude_model_returns_200k(self) -> None:
        """Claude models without SDK usage should return 200K."""
        assert resolve_context_window("claude-opus-4-6", None) == CLAUDE_DEFAULT_CONTEXT_WINDOW
        assert resolve_context_window("claude-sonnet-4-6", None) == CLAUDE_DEFAULT_CONTEXT_WINDOW
        assert resolve_context_window("claude-haiku-4-5", None) == CLAUDE_DEFAULT_CONTEXT_WINDOW

    def test_claude_name_variations(self) -> None:
        """Various Claude model name formats should all return 200K."""
        names = [
            "opus",
            "sonnet",
            "haiku",
            "claude-3-5-sonnet-20241022",
            "claude-3-opus-20240229",
            "claude-3-haiku-20240307",
            "anthropic/claude-sonnet-4-6",
        ]
        for name in names:
            result = resolve_context_window(name, None)
            assert result == CLAUDE_DEFAULT_CONTEXT_WINDOW, f"Failed for model name: {name}"

    def test_non_claude_model_uses_litellm(self) -> None:
        """Non-Claude models should fall through to litellm."""
        mock_litellm = MagicMock()
        mock_litellm.get_model_info.return_value = {"max_input_tokens": 128_000}

        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = resolve_context_window("gpt-4o", None)

        assert result == 128_000
        mock_litellm.get_model_info.assert_called_once_with(model="gpt-4o")

    def test_litellm_failure_returns_none(self) -> None:
        """If litellm fails for a non-Claude model, return None."""
        mock_litellm = MagicMock()
        mock_litellm.get_model_info.side_effect = KeyError("Unknown model")

        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = resolve_context_window("unknown-model-xyz", None)

        assert result is None

    def test_none_model_returns_none(self) -> None:
        """None model with no SDK usage should return None."""
        assert resolve_context_window(None, None) is None

    def test_none_model_with_sdk_usage(self) -> None:
        """None model but with SDK usage should return the SDK value."""
        model_usage = {"contextWindow": 200_000}
        result = resolve_context_window(None, model_usage)
        assert result == 200_000

    def test_empty_model_usage_dict(self) -> None:
        """Empty model_usage dict (no contextWindow key) should fall through."""
        result = resolve_context_window("claude-opus-4-6", {})
        assert result == CLAUDE_DEFAULT_CONTEXT_WINDOW

    def test_claude_model_never_calls_litellm(self) -> None:
        """Claude models should never reach the litellm fallback path."""
        mock_litellm = MagicMock()

        with patch.dict("sys.modules", {"litellm": mock_litellm}):
            result = resolve_context_window("claude-sonnet-4-6", None)

        assert result == CLAUDE_DEFAULT_CONTEXT_WINDOW
        mock_litellm.get_model_info.assert_not_called()
