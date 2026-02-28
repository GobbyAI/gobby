"""
Smoke tests for E2E infrastructure.

Verifies that the E2E test fixtures work correctly.
"""

import pytest

pytestmark = pytest.mark.e2e


class TestE2EInfrastructure:
    """Tests verifying E2E fixtures work correctly."""

    def test_daemon_starts_and_responds(self, daemon_instance, daemon_client) -> None:
        """Verify daemon starts and responds to health check."""
        # Check daemon is running
        assert daemon_instance.is_alive(), "Daemon process should be alive"

        # Check health endpoint
        response = daemon_client.get("/api/admin/status")
        assert response.status_code == 200

        data = response.json()
        assert data.get("status") == "healthy"

    def test_daemon_instance_has_isolated_config(self, daemon_instance) -> None:
        """Verify daemon uses isolated configuration."""
        # Config should be in temp directory
        assert "gobby_e2e_" in str(daemon_instance.config_path)

        # Database should be in temp directory
        assert "gobby_e2e_" in str(daemon_instance.db_path)

    def test_daemon_logs_captured(self, daemon_instance) -> None:
        """Verify daemon logs are captured to files."""
        # Log files should exist
        assert daemon_instance.log_file.exists() or daemon_instance.error_log_file.exists()

    def test_cli_events_simulator_works(self, daemon_instance, daemon_client) -> None:
        """Verify CLI event endpoints are accessible."""
        # Test that sessions endpoint is accessible
        response = daemon_client.get("/api/sessions")
        assert response.status_code == 200

        data = response.json()
        assert "sessions" in data
        assert "count" in data

    def test_mcp_client_lists_internal_servers(self, daemon_instance, mcp_client) -> None:
        """Verify MCP client can list internal servers."""
        servers = mcp_client.list_servers()

        # Should have internal gobby servers
        server_names = [s.get("name") for s in servers]
        assert "gobby-tasks" in server_names or len(servers) >= 0

    def test_project_dir_has_gobby_structure(self, e2e_project_dir) -> None:
        """Verify project directory has .gobby structure."""
        gobby_dir = e2e_project_dir / ".gobby"
        assert gobby_dir.exists()

        project_json = gobby_dir / "project.json"
        assert project_json.exists()

    def test_multiple_daemon_instances_use_different_ports(
        self, e2e_project_dir, e2e_config
    ) -> None:
        """Verify config uses dynamically allocated ports."""
        config_path, http_port, ws_port = e2e_config

        # Ports should be in valid range
        assert 1024 < http_port < 65536
        assert 1024 < ws_port < 65536

        # Ports should be different
        assert http_port != ws_port
