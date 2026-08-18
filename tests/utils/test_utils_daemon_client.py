"""Tests for src/utils/daemon_client.py - Daemon HTTP Client."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent
from gobby.utils.daemon_client import DaemonClient, DaemonHealthError
from gobby.utils.daemon_url import DaemonUrlError

pytestmark = pytest.mark.unit


class TestDaemonClientInit:
    """Tests for DaemonClient initialization."""

    def test_default_values(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test default initialization resolves the configured daemon port."""
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        monkeypatch.delenv("GOBBY_DAEMON_URL", raising=False)
        monkeypatch.delenv("GOBBY_PORT", raising=False)
        monkeypatch.delenv("GOBBY_DAEMON_PORT", raising=False)
        bootstrap_path = tmp_path / "bootstrap.yaml"
        files_home = tmp_path / "files"
        files_home.mkdir()
        bootstrap_path.write_text(
            f"daemon_port: 61999\nfiles_home: {files_home}\n", encoding="utf-8"
        )
        bootstrap_path.chmod(0o600)

        client = DaemonClient()

        assert client.url == "http://127.0.0.1:61999"
        assert client.timeout == 5.0

    def test_custom_values(self) -> None:
        """Test custom initialization values."""
        client = DaemonClient(host="192.168.1.1", port=9000, timeout=10.0)

        assert client.url == "http://192.168.1.1:9000"
        assert client.timeout == 10.0

    def test_explicit_localhost_uses_numeric_loopback(self) -> None:
        client = DaemonClient(host="localhost", port=9000)

        assert client.url == "http://127.0.0.1:9000"

    def test_url_constructor(self) -> None:
        """Test URL-based initialization values."""
        client = DaemonClient.from_url("http://daemon.example.test:61999/", timeout=10.0)

        assert client.url == "http://daemon.example.test:61999"
        assert client.timeout == 10.0

    def test_url_constructor_rejects_invalid_url(self) -> None:
        """Test URL-based initialization validates the URL."""
        with pytest.raises(DaemonUrlError):
            DaemonClient.from_url("ftp://daemon.example.test:61999")

    def test_url_constructor_rejects_empty_url(self) -> None:
        """Test explicit empty URL does not fall back to localhost."""
        with pytest.raises(DaemonUrlError):
            DaemonClient.from_url("")

    def test_custom_logger(self) -> None:
        """Test with custom logger."""
        mock_logger = MagicMock()
        client = DaemonClient(logger=mock_logger)

        assert client.logger is mock_logger

    def test_status_text_mapping(self) -> None:
        """Test DAEMON_STATUS_TEXT class constant."""
        assert DaemonClient.DAEMON_STATUS_TEXT["not_running"] == "Not Running"
        assert DaemonClient.DAEMON_STATUS_TEXT["cannot_access"] == "Cannot Access"
        assert DaemonClient.DAEMON_STATUS_TEXT["ready"] == "Ready"


class TestDaemonClientCheckHealth:
    """Tests for check_health method."""

    def test_health_check_success(self) -> None:
        """Test successful health check."""
        logger = MagicMock()
        client = DaemonClient(logger=logger)

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.get", return_value=mock_response) as mock_get:
            is_healthy, error = client.check_health()

        assert is_healthy is True
        assert error is None
        assert mock_get.call_args.kwargs["trust_env"] is False
        logger.debug.assert_called_once_with(
            "Daemon health check passed",
            extra={
                "url": "http://127.0.0.1:60887",
                "health_failed_since_last_success": False,
            },
        )
        logger.info.assert_not_called()

    def test_health_check_non_200_status(self) -> None:
        """Test health check with non-200 status."""
        logger = MagicMock()
        client = DaemonClient(logger=logger)

        mock_response = MagicMock()
        mock_response.status_code = 503

        with patch("httpx.get", return_value=mock_response):
            is_healthy, error = client.check_health()

        assert is_healthy is False
        assert error == "HTTP 503"
        logger.warning.assert_called_once_with("Daemon health check failed: status %s", 503)

    def test_health_check_connection_refused_is_debug_only(self) -> None:
        """A retryable connection refusal does not emit a warning."""
        logger = MagicMock()
        client = DaemonClient(logger=logger)

        with patch("httpx.get", side_effect=httpx.ConnectError("Connection refused")):
            is_healthy, error = client.check_health()

        assert is_healthy is False
        assert error is DaemonHealthError.NOT_RUNNING
        logger.warning.assert_not_called()
        logger.debug.assert_called_once()

    def test_single_health_timeout_is_debug_only(self) -> None:
        """A transient timeout remains observable without warning noise."""
        logger = MagicMock()
        client = DaemonClient(logger=logger)
        timeout = httpx.ReadTimeout("timed out")

        with patch("httpx.get", side_effect=timeout):
            assert client.check_health() == (False, "timed out")

        logger.warning.assert_not_called()
        logger.debug.assert_called_once_with(
            "Daemon health check timed out",
            extra={
                "daemon_url": "http://127.0.0.1:60887",
                "timeout_streak": 1,
                "error": "timed out",
            },
        )

    def test_repeated_health_timeouts_warn_once(self) -> None:
        """Consecutive timeouts escalate once so persistent hangs stay visible."""
        logger = MagicMock()
        client = DaemonClient(logger=logger)
        timeout = httpx.ReadTimeout("timed out")

        with patch("httpx.get", side_effect=timeout):
            assert client.check_health() == (False, "timed out")
            assert client.check_health() == (False, "timed out")
            assert client.check_health() == (False, "timed out")

        logger.warning.assert_called_once_with(
            "Daemon health check timed out twice consecutively",
            extra={
                "daemon_url": "http://127.0.0.1:60887",
                "timeout_streak": 2,
                "error": "timed out",
            },
        )

    def test_health_success_resets_timeout_streak(self) -> None:
        """Separated transient timeouts never escalate to a warning."""
        logger = MagicMock()
        client = DaemonClient(logger=logger)
        timeout = httpx.ReadTimeout("timed out")
        success = MagicMock(status_code=200)

        with patch("httpx.get", side_effect=[timeout, success, timeout]):
            assert client.check_health() == (False, "timed out")
            assert client.check_health() == (True, None)
            assert client.check_health() == (False, "timed out")

        logger.warning.assert_not_called()

    def test_health_check_recovery_logs_info_once(self) -> None:
        """Recovery after a failed health state is logged once at info level."""
        logger = MagicMock()
        client = DaemonClient(logger=logger)
        failure_response = MagicMock()
        failure_response.status_code = 503
        success_response = MagicMock()
        success_response.status_code = 200

        with patch("httpx.get", side_effect=[failure_response, success_response, success_response]):
            assert client.check_health() == (False, "HTTP 503")
            assert client.check_health() == (True, None)
            assert client.check_health() == (True, None)

        logger.info.assert_called_once_with(
            "Daemon health recovered",
            extra={
                "url": "http://127.0.0.1:60887",
                "health_failed_since_last_success": True,
            },
        )
        logger.debug.assert_called_once_with(
            "Daemon health check passed",
            extra={
                "url": "http://127.0.0.1:60887",
                "health_failed_since_last_success": False,
            },
        )

    def test_health_check_connection_refused_during_planned_restart_does_not_warn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Planned restarts make transient daemon gaps expected."""
        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, home=tmp_path)
        logger = MagicMock()
        client = DaemonClient(logger=logger)

        with patch("httpx.get", side_effect=httpx.ConnectError("Connection refused")):
            is_healthy, error = client.check_health()

        assert is_healthy is False
        assert error is DaemonHealthError.NOT_RUNNING
        logger.warning.assert_not_called()
        logger.debug.assert_called_once()
        debug_args = logger.debug.call_args.args
        assert "during planned restart (cli_restart)" in debug_args[0] % debug_args[1:]

    @pytest.mark.parametrize(
        "error",
        [
            httpx.ReadError("Connection reset by peer"),
            httpx.ConnectTimeout("Connection timed out"),
        ],
    )
    def test_health_check_other_http_error(self, error: httpx.HTTPError) -> None:
        """HTTP failures other than ConnectError preserve their diagnostic."""
        client = DaemonClient()

        with patch("httpx.get", side_effect=error):
            is_healthy, health_error = client.check_health()

        assert is_healthy is False
        assert health_error == str(error)


class TestDaemonClientCheckStatus:
    """Tests for check_status method."""

    def test_status_ready(self) -> None:
        """Test status when daemon is ready."""
        client = DaemonClient()

        with patch.object(client, "check_health", return_value=(True, None)):
            is_ready, message, status, error = client.check_status()

        assert is_ready is True
        assert message == "Daemon is ready"
        assert status == "ready"
        assert error is None

    def test_status_not_running(self) -> None:
        """Test status when daemon is not running."""
        client = DaemonClient()

        with patch.object(
            client,
            "check_health",
            return_value=(False, DaemonHealthError.NOT_RUNNING),
        ):
            is_ready, message, status, error = client.check_status()

        assert is_ready is False
        assert message == "Daemon is not running"
        assert status == "not_running"
        assert error == "Daemon is not running"

    def test_status_cannot_access(self) -> None:
        """Test status when daemon cannot be accessed."""
        client = DaemonClient()

        with patch.object(client, "check_health", return_value=(False, "HTTP 503")):
            is_ready, message, status, error = client.check_status()

        assert is_ready is False
        assert message is not None
        assert "Cannot access daemon" in message
        assert status == "cannot_access"
        assert error == "HTTP 503"

    @pytest.mark.parametrize(
        "health_error",
        [
            httpx.ReadError("Connection reset by peer"),
            httpx.ConnectTimeout("Connection timed out"),
        ],
    )
    def test_http_error_status_is_cannot_access(self, health_error: httpx.HTTPError) -> None:
        """Transport and timeout failures report cannot-access status."""
        client = DaemonClient()

        with patch("httpx.get", side_effect=health_error):
            is_ready, message, status, error = client.check_status()

        assert is_ready is False
        assert message == f"Cannot access daemon: {health_error}"
        assert status == "cannot_access"
        assert error == str(health_error)


class TestDaemonClientCallHttpApi:
    """Tests for call_http_api method."""

    def test_get_request(self) -> None:
        """Test GET request."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.get", return_value=mock_response) as mock_get:
            response = client.call_http_api("/test", method="GET")

        assert response == mock_response
        mock_get.assert_called_once()

    def test_post_request(self) -> None:
        """Test POST request with JSON data."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.post", return_value=mock_response) as mock_post:
            response = client.call_http_api(
                "/sessions/register", method="POST", json_data={"cli_key": "test-123"}
            )

        assert response == mock_response
        mock_post.assert_called_once()

    def test_put_request(self) -> None:
        """Test PUT request."""
        client = DaemonClient()

        mock_response = MagicMock()

        with patch("httpx.put", return_value=mock_response) as mock_put:
            response = client.call_http_api("/update", method="PUT", json_data={"key": "value"})

        assert response == mock_response
        mock_put.assert_called_once()

    def test_patch_request(self) -> None:
        """Test PATCH request."""
        client = DaemonClient()
        mock_response = MagicMock()

        with patch("httpx.patch", return_value=mock_response) as mock_patch:
            response = client.call_http_api(
                "/update",
                method="PATCH",
                json_data={"key": "value"},
            )

        assert response == mock_response
        mock_patch.assert_called_once()

    def test_delete_request(self) -> None:
        """Test DELETE request."""
        client = DaemonClient()

        mock_response = MagicMock()

        with patch("httpx.delete", return_value=mock_response) as mock_delete:
            response = client.call_http_api("/resource/123", method="DELETE")

        assert response == mock_response
        mock_delete.assert_called_once()

    def test_unsupported_method(self) -> None:
        """Test unsupported HTTP method raises ValueError."""
        client = DaemonClient()

        with pytest.raises(ValueError, match="Unsupported HTTP method"):
            client.call_http_api("/test", method="OPTIONS")

    def test_custom_timeout(self) -> None:
        """Test using custom timeout."""
        client = DaemonClient(timeout=5.0)

        mock_response = MagicMock()

        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.call_http_api("/test", method="GET", timeout=30.0)

        # Verify custom timeout was used
        call_args = mock_get.call_args
        assert call_args.kwargs["timeout"] == 30.0

    def test_zero_timeout_is_preserved(self) -> None:
        client = DaemonClient(timeout=5.0)

        with patch("httpx.get", return_value=MagicMock()) as mock_get:
            client.call_http_api("/test", method="GET", timeout=0)

        assert mock_get.call_args.kwargs["timeout"] == 0

    @pytest.mark.parametrize("method", ["GET", "DELETE"])
    def test_request_body_is_preserved(self, method: str) -> None:
        client = DaemonClient()
        payload = {"key": "value"}

        with patch("httpx.request", return_value=MagicMock()) as mock_request:
            client.call_http_api("/test", method=method, json_data=payload)

        assert mock_request.call_args.args[:2] == (method, f"{client.url}/test")
        assert mock_request.call_args.kwargs["json"] == payload

    def test_exception_handling(self) -> None:
        """Test exception is raised on failure."""
        client = DaemonClient()

        with patch("httpx.post", side_effect=Exception("Network error")):
            with pytest.raises(Exception, match="Network error"):
                client.call_http_api("/test", method="POST")


class TestDaemonClientCallMcpTool:
    """Tests for call_mcp_tool method."""

    def test_call_mcp_tool_success(self) -> None:
        """Test successful MCP tool call."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"result": "success"}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "call_http_api", return_value=mock_response):
            result = client.call_mcp_tool(
                server_name="context7",
                tool_name="get-library-docs",
                arguments={"libraryId": "/react/react"},
            )

        assert result == {"result": "success"}

    def test_call_mcp_tool_endpoint_format(self) -> None:
        """Test that correct endpoint is constructed."""
        client = DaemonClient()

        mock_response = MagicMock()
        mock_response.json.return_value = {}
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "call_http_api", return_value=mock_response) as mock_call:
            client.call_mcp_tool("supabase", "list_tables", {"schemas": ["public"]})

        mock_call.assert_called_once_with(
            endpoint="/api/mcp/supabase/tools/list_tables",
            method="POST",
            json_data={"schemas": ["public"]},
            timeout=None,
        )
        assert mock_call.call_count == 1
        assert mock_call.call_args is not None
