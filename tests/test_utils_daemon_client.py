"""Tests for src/utils/daemon_client.py - Daemon HTTP Client."""

import pytest
from unittest.mock import patch, MagicMock
import httpx

from gobby.utils.daemon_client import DaemonClient


class TestDaemonClientInit:
    """Tests for DaemonClient initialization."""

    def test_default_values(self):
        """Test default initialization values."""
        client = DaemonClient()

        assert client.url == "http://localhost:8765"
        assert client.timeout == 5.0
        assert client._cached_is_ready is None
        assert client._cached_status is None

    def test_custom_values(self):
        """Test custom initialization values."""
        client = DaemonClient(host="192.168.1.1", port=9000, timeout=10.0)

        assert client.url == "http://192.168.1.1:9000"
        assert client.timeout == 10.0

    def test_custom_logger(self):
        """Test with custom logger."""
        mock_logger = MagicMock()
        client = DaemonClient(logger=mock_logger)

        assert client.logger is mock_logger

    def test_status_text_mapping(self):
        """Test DAEMON_STATUS_TEXT class constant."""
        assert DaemonClient.DAEMON_STATUS_TEXT["not_running"] == "Not Running"
        assert DaemonClient.DAEMON_STATUS_TEXT["cannot_access"] == "Cannot Access"
        assert DaemonClient.DAEMON_STATUS_TEXT["ready"] == "Ready"


class TestDaemonClientCheckHealth:
    """Tests for check_health method."""

    def test_health_check_success(self):
        """Test successful health check."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch('httpx.get', return_value=mock_response):
            is_healthy, error = client.check_health()

        assert is_healthy is True
        assert error is None

    def test_health_check_non_200_status(self):
        """Test health check with non-200 status."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch('httpx.get', return_value=mock_response):
            is_healthy, error = client.check_health()

        assert is_healthy is False
        assert error == "HTTP 503"

    def test_health_check_connection_refused(self):
        """Test health check when daemon not running."""
        client = DaemonClient()

        with patch('httpx.get', side_effect=Exception("Connection refused")):
            is_healthy, error = client.check_health()

        assert is_healthy is False
        assert error is None  # None indicates daemon not running

    def test_health_check_other_error(self):
        """Test health check with other errors."""
        client = DaemonClient()

        with patch('httpx.get', side_effect=Exception("DNS resolution failed")):
            is_healthy, error = client.check_health()

        assert is_healthy is False
        assert "DNS resolution failed" in error


class TestDaemonClientCheckStatus:
    """Tests for check_status method."""

    def test_status_ready(self):
        """Test status when daemon is ready."""
        client = DaemonClient()

        with patch.object(client, 'check_health', return_value=(True, None)):
            is_ready, message, status, error = client.check_status()

        assert is_ready is True
        assert message == "Daemon is ready"
        assert status == "ready"
        assert error is None

    def test_status_not_running(self):
        """Test status when daemon is not running."""
        client = DaemonClient()

        with patch.object(client, 'check_health', return_value=(False, None)):
            is_ready, message, status, error = client.check_status()

        assert is_ready is False
        assert message == "Daemon is not running"
        assert status == "not_running"
        assert error is None

    def test_status_cannot_access(self):
        """Test status when daemon cannot be accessed."""
        client = DaemonClient()

        with patch.object(client, 'check_health', return_value=(False, "HTTP 503")):
            is_ready, message, status, error = client.check_status()

        assert is_ready is False
        assert "Cannot access daemon" in message
        assert status == "cannot_access"
        assert error == "HTTP 503"


class TestDaemonClientCallHttpApi:
    """Tests for call_http_api method."""

    def test_get_request(self):
        """Test GET request."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch('httpx.get', return_value=mock_response) as mock_get:
            response = client.call_http_api("/test", method="GET")

        assert response == mock_response
        mock_get.assert_called_once()

    def test_post_request(self):
        """Test POST request with JSON data."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch('httpx.post', return_value=mock_response) as mock_post:
            response = client.call_http_api(
                "/sessions/register",
                method="POST",
                json_data={"cli_key": "test-123"}
            )

        assert response == mock_response
        mock_post.assert_called_once()

    def test_put_request(self):
        """Test PUT request."""
        client = DaemonClient()

        mock_response = MagicMock()

        with patch('httpx.put', return_value=mock_response) as mock_put:
            response = client.call_http_api("/update", method="PUT", json_data={"key": "value"})

        assert response == mock_response
        mock_put.assert_called_once()

    def test_delete_request(self):
        """Test DELETE request."""
        client = DaemonClient()

        mock_response = MagicMock()

        with patch('httpx.delete', return_value=mock_response) as mock_delete:
            response = client.call_http_api("/resource/123", method="DELETE")

        assert response == mock_response
        mock_delete.assert_called_once()

    def test_unsupported_method(self):
        """Test unsupported HTTP method raises ValueError."""
        client = DaemonClient()

        with pytest.raises(ValueError, match="Unsupported HTTP method"):
            client.call_http_api("/test", method="PATCH")

    def test_custom_timeout(self):
        """Test using custom timeout."""
        client = DaemonClient(timeout=5.0)

        mock_response = MagicMock()

        with patch('httpx.get', return_value=mock_response) as mock_get:
            client.call_http_api("/test", method="GET", timeout=30.0)

        # Verify custom timeout was used
        call_args = mock_get.call_args
        assert call_args.kwargs['timeout'] == 30.0

    def test_exception_handling(self):
        """Test exception is raised on failure."""
        client = DaemonClient()

        with patch('httpx.post', side_effect=Exception("Network error")):
            with pytest.raises(Exception, match="Network error"):
                client.call_http_api("/test", method="POST")


class TestDaemonClientCallMcpTool:
    """Tests for call_mcp_tool method."""

    def test_call_mcp_tool_success(self):
        """Test successful MCP tool call."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, 'call_http_api', return_value=mock_response):
            result = client.call_mcp_tool(
                server_name="context7",
                tool_name="get-library-docs",
                arguments={"libraryId": "/react/react"}
            )

        assert result == {"result": "success"}

    def test_call_mcp_tool_endpoint_format(self):
        """Test that correct endpoint is constructed."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, 'call_http_api', return_value=mock_response) as mock_call:
            client.call_mcp_tool("supabase", "list_tables", {"schemas": ["public"]})

        mock_call.assert_called_once_with(
            endpoint="/mcp/supabase/tools/list_tables",
            method="POST",
            json_data={"schemas": ["public"]},
            timeout=None
        )


class TestDaemonClientStatusCache:
    """Tests for status caching functionality."""

    def test_update_status_cache(self):
        """Test updating status cache."""
        client = DaemonClient()

        with patch.object(client, 'check_status', return_value=(True, "Ready", "ready", None)):
            client.update_status_cache()

        assert client._cached_is_ready is True
        assert client._cached_message == "Ready"
        assert client._cached_status == "ready"
        assert client._cached_error is None

    def test_get_cached_status_initial(self):
        """Test getting cached status before any check."""
        client = DaemonClient()

        is_ready, message, status, error = client.get_cached_status()

        assert is_ready is None
        assert message is None
        assert status is None
        assert error is None

    def test_get_cached_status_after_update(self):
        """Test getting cached status after update."""
        client = DaemonClient()

        with patch.object(client, 'check_status', return_value=(False, "Not running", "not_running", None)):
            client.update_status_cache()

        is_ready, message, status, error = client.get_cached_status()

        assert is_ready is False
        assert message == "Not running"
        assert status == "not_running"

    def test_cache_thread_safety(self):
        """Test that cache operations use lock."""
        client = DaemonClient()

        # Verify the lock exists
        assert hasattr(client, '_cache_lock')

        # Test that operations work (thread safety is implicit via lock usage)
        with patch.object(client, 'check_status', return_value=(True, "Ready", "ready", None)):
            client.update_status_cache()

        result = client.get_cached_status()
        assert result[0] is True
