"""Core tests for Gemini CLI adapter."""

from unittest.mock import MagicMock

import pytest

from gobby.adapters.gemini import GeminiAdapter
from gobby.hooks.events import SessionSource

pytestmark = pytest.mark.unit


class TestGeminiAdapterInit:
    """Tests for GeminiAdapter initialization."""

    def test_init_without_hook_manager(self) -> None:
        """GeminiAdapter initializes without hook_manager."""
        adapter = GeminiAdapter()
        assert adapter._hook_manager is None

    def test_init_with_hook_manager(self) -> None:
        """GeminiAdapter stores hook_manager reference."""
        mock_manager = MagicMock()
        adapter = GeminiAdapter(hook_manager=mock_manager)
        assert adapter._hook_manager is mock_manager

    def test_source_is_gemini(self) -> None:
        """GeminiAdapter reports GEMINI as source."""
        adapter = GeminiAdapter()
        assert adapter.source == SessionSource.GEMINI
