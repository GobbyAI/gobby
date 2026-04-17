"""Tests for GitHubIntegration class.

Tests verify that GitHubIntegration correctly detects GitHub MCP server availability
and provides graceful error messages when unavailable.

TDD Red Phase: Tests should fail initially since GitHubIntegration class does not exist.
"""

import time
from unittest.mock import MagicMock

import pytest

from gobby.integrations.github import GitHubIntegration

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCPClientManager."""
    manager = MagicMock()
    manager.has_server = MagicMock(return_value=True)
    manager.health = {
        "github": MagicMock(state="connected"),
    }
    return manager


@pytest.fixture
def github_integration(mock_mcp_manager):
    """Create a GitHubIntegration instance with mock manager."""
    return GitHubIntegration(mock_mcp_manager)


class TestGitHubIntegrationAvailability:
    """Test is_available() method."""

    def test_is_available_returns_true_when_configured_and_connected(
        self, mock_mcp_manager
    ) -> None:
        """is_available() returns True when GitHub MCP server is configured and connected."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}

        integration = GitHubIntegration(mock_mcp_manager)
        assert integration.is_available() is True

    def test_is_available_returns_false_when_not_configured(self, mock_mcp_manager) -> None:
        """is_available() returns False when GitHub MCP server is not configured."""
        mock_mcp_manager.has_server.return_value = False

        integration = GitHubIntegration(mock_mcp_manager)
        assert integration.is_available() is False

    def test_is_available_returns_false_when_disconnected(self, mock_mcp_manager) -> None:
        """is_available() returns False when GitHub MCP server is configured but disconnected."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="disconnected")}

        integration = GitHubIntegration(mock_mcp_manager)
        assert integration.is_available() is False

    def test_is_available_returns_false_when_health_missing(self, mock_mcp_manager) -> None:
        """is_available() returns False when health info is missing for github server."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {}  # No github entry

        integration = GitHubIntegration(mock_mcp_manager)
        assert integration.is_available() is False


class TestGitHubIntegrationCaching:
    """Test availability caching behavior."""

    def test_availability_is_cached(self, mock_mcp_manager) -> None:
        """Repeated is_available() calls use cached result within cache window."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}

        integration = GitHubIntegration(mock_mcp_manager, cache_ttl_seconds=60)

        # First call
        result1 = integration.is_available()
        # Second call should use cache
        result2 = integration.is_available()

        assert result1 is True
        assert result2 is True
        # has_server should only be called once due to caching
        assert mock_mcp_manager.has_server.call_count == 1

    def test_cache_expires_after_ttl(self, mock_mcp_manager) -> None:
        """Calls after cache timeout trigger new MCP checks."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}

        # Use very short TTL for testing
        integration = GitHubIntegration(mock_mcp_manager, cache_ttl_seconds=0.1)

        # First call
        integration.is_available()
        first_call_count = mock_mcp_manager.has_server.call_count

        # Wait for cache to expire
        time.sleep(0.15)

        # Second call should check again
        integration.is_available()
        second_call_count = mock_mcp_manager.has_server.call_count

        assert second_call_count > first_call_count

    def test_cache_can_be_cleared(self, mock_mcp_manager) -> None:
        """clear_cache() forces next is_available() to check fresh."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}

        integration = GitHubIntegration(mock_mcp_manager, cache_ttl_seconds=60)

        # First call
        integration.is_available()
        first_call_count = mock_mcp_manager.has_server.call_count

        # Clear cache
        integration.clear_cache()

        # Next call should check again
        integration.is_available()
        second_call_count = mock_mcp_manager.has_server.call_count

        assert second_call_count > first_call_count


class TestGitHubIntegrationErrorMessages:
    """Test graceful error message generation."""

    def test_unavailable_reason_when_not_configured(self, mock_mcp_manager) -> None:
        """get_unavailable_reason() explains when server not configured."""
        mock_mcp_manager.has_server.return_value = False

        integration = GitHubIntegration(mock_mcp_manager)
        reason = integration.get_unavailable_reason()

        assert reason is not None
        assert "not configured" in reason.lower() or "not found" in reason.lower()

    def test_unavailable_reason_when_disconnected(self, mock_mcp_manager) -> None:
        """get_unavailable_reason() explains when server is disconnected."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="disconnected")}

        integration = GitHubIntegration(mock_mcp_manager)
        reason = integration.get_unavailable_reason()

        assert reason is not None
        assert "disconnected" in reason.lower() or "not connected" in reason.lower()

    def test_unavailable_reason_returns_none_when_available(self, mock_mcp_manager) -> None:
        """get_unavailable_reason() returns None when server is available."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}

        integration = GitHubIntegration(mock_mcp_manager)
        reason = integration.get_unavailable_reason()

        assert reason is None

    def test_require_available_raises_when_unavailable(self, mock_mcp_manager) -> None:
        """require_available() raises RuntimeError when GitHub MCP unavailable."""
        mock_mcp_manager.has_server.return_value = False

        integration = GitHubIntegration(mock_mcp_manager)

        with pytest.raises(RuntimeError) as exc_info:
            integration.require_available()

        assert "github" in str(exc_info.value).lower()

    def test_require_available_succeeds_when_available(self, mock_mcp_manager) -> None:
        """require_available() returns without error when GitHub MCP available."""
        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github": MagicMock(state="connected")}

        integration = GitHubIntegration(mock_mcp_manager)
        # Should not raise
        integration.require_available()


class TestGitHubIntegrationServerName:
    """Test server name configuration."""

    def test_default_server_name_is_github(self, mock_mcp_manager) -> None:
        """Default server name should be 'github'."""
        integration = GitHubIntegration(mock_mcp_manager)
        assert integration.server_name == "github"

    def test_custom_server_name(self, mock_mcp_manager) -> None:
        """Server name can be customized."""
        integration = GitHubIntegration(mock_mcp_manager, server_name="github-custom")
        assert integration.server_name == "github-custom"
        mock_mcp_manager.has_server.assert_not_called()  # Not called until is_available()

        mock_mcp_manager.has_server.return_value = True
        mock_mcp_manager.health = {"github-custom": MagicMock(state="connected")}
        integration.is_available()
        mock_mcp_manager.has_server.assert_called_with("github-custom")
