"""Tests for src/utils/status.py - Status Message Formatting."""

import pytest

from gobby.utils.status import format_status_message


class TestFormatStatusMessage:
    """Tests for format_status_message function."""

    def test_stopped_status(self):
        """Test formatting stopped daemon status."""
        result = format_status_message(running=False)

        assert "GOBBY DAEMON STATUS" in result
        assert "Status: Stopped" in result
        assert "=" * 70 in result

    def test_running_status_minimal(self):
        """Test running status with minimal info."""
        result = format_status_message(running=True)

        assert "Status: Running" in result
        assert "PID:" not in result  # No PID provided

    def test_running_status_with_pid(self):
        """Test running status with PID."""
        result = format_status_message(running=True, pid=12345)

        assert "Status: Running" in result
        assert "PID: 12345" in result

    def test_running_status_with_uptime(self):
        """Test running status with uptime."""
        result = format_status_message(running=True, uptime="1h 23m 45s")

        assert "Uptime: 1h 23m 45s" in result

    def test_running_status_with_pid_file(self):
        """Test running status with PID file path."""
        result = format_status_message(
            running=True,
            pid_file="/var/run/gobby.pid"
        )

        assert "PID file: /var/run/gobby.pid" in result

    def test_running_status_with_log_files(self):
        """Test running status with log files path."""
        result = format_status_message(
            running=True,
            log_files="/var/log/gobby/"
        )

        assert "Log files: /var/log/gobby/" in result

    def test_server_configuration_with_http_port(self):
        """Test server configuration section with HTTP port."""
        result = format_status_message(
            running=True,
            http_port=8765
        )

        assert "Server Configuration:" in result
        assert "HTTP Port: 8765" in result

    def test_server_configuration_with_websocket_port(self):
        """Test server configuration section with WebSocket port."""
        result = format_status_message(
            running=True,
            websocket_port=8766
        )

        assert "Server Configuration:" in result
        assert "WebSocket Port: 8766" in result

    def test_server_configuration_with_both_ports(self):
        """Test server configuration with both ports."""
        result = format_status_message(
            running=True,
            http_port=8765,
            websocket_port=8766
        )

        assert "HTTP Port: 8765" in result
        assert "WebSocket Port: 8766" in result

    def test_no_server_configuration_when_no_ports(self):
        """Test that server configuration section is hidden when no ports."""
        result = format_status_message(running=True, pid=123)

        assert "Server Configuration:" not in result

    def test_full_status_message(self):
        """Test full status message with all fields."""
        result = format_status_message(
            running=True,
            pid=54321,
            pid_file="/home/user/.gobby/daemon.pid",
            log_files="/home/user/.gobby/logs/",
            uptime="2h 30m 15s",
            http_port=8765,
            websocket_port=8766
        )

        # Header
        assert "=" * 70 in result
        assert "GOBBY DAEMON STATUS" in result

        # Status section
        assert "Status: Running" in result
        assert "PID: 54321" in result
        assert "Uptime: 2h 30m 15s" in result
        assert "PID file: /home/user/.gobby/daemon.pid" in result
        assert "Log files: /home/user/.gobby/logs/" in result

        # Server configuration section
        assert "Server Configuration:" in result
        assert "HTTP Port: 8765" in result
        assert "WebSocket Port: 8766" in result

    def test_stopped_status_no_details(self):
        """Test stopped status doesn't show running details."""
        result = format_status_message(
            running=False,
            pid=12345,  # Should be ignored
            uptime="1h",  # Should be ignored
        )

        assert "Status: Stopped" in result
        assert "PID: 12345" not in result
        assert "Uptime:" not in result

    def test_extra_kwargs_ignored(self):
        """Test that extra kwargs are silently ignored."""
        # Should not raise any exception
        result = format_status_message(
            running=True,
            unknown_field="value",
            another_unknown=123
        )

        assert "Status: Running" in result

    def test_output_is_string(self):
        """Test that output is a string."""
        result = format_status_message(running=True)

        assert isinstance(result, str)

    def test_output_has_newlines(self):
        """Test that output uses newlines for formatting."""
        result = format_status_message(running=True, pid=123)

        assert "\n" in result
        lines = result.split("\n")
        assert len(lines) > 5  # Should have multiple lines
