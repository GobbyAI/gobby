"""Shared fixtures for adapter tests."""

from unittest.mock import MagicMock

import pytest

from gobby.adapters.gemini import GeminiAdapter
from gobby.hooks.events import HookResponse

pytestmark = pytest.mark.unit


@pytest.fixture
def adapter() -> GeminiAdapter:
    """Create a GeminiAdapter instance."""
    return GeminiAdapter()


@pytest.fixture
def mock_hook_manager() -> MagicMock:
    """Create a mock HookManager with an allow response."""
    manager = MagicMock()
    manager.handle.return_value = HookResponse(decision="allow")
    return manager
