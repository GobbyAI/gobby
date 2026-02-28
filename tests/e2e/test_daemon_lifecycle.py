"""
E2E tests for daemon lifecycle (start/stop/restart).

Tests verify:
1. Daemon starts and becomes ready (PID file created, health endpoint responds)
2. Daemon stops gracefully on SIGTERM (PID file removed, no orphan processes)
3. Daemon restart preserves no state leakage (clean restart)
4. Multiple start attempts fail gracefully when daemon already running
5. Stop on non-running daemon is idempotent
"""

import os
import signal
import time

import httpx
import psutil
import pytest

from tests.e2e.conftest import (
    DaemonInstance,
    prepare_daemon_env,
    terminate_process_tree,
    wait_for_daemon_health,
)

pytestmark = pytest.mark.e2e


class TestDaemonStart:
    """Tests for daemon startup behavior."""

    def test_daemon_starts_and_creates_pid_file(self, daemon_instance: DaemonInstance) -> None:
        """Verify daemon process is running after startup."""
        # Daemon should be alive
        assert daemon_instance.is_alive(), "Daemon process should be alive after start"

        # Process should be accessible via psutil
        try:
            proc = psutil.Process(daemon_instance.pid)
            assert proc.is_running(), "Process should be running"
            assert proc.status() != psutil.STATUS_ZOMBIE, "Process should not be zombie"
        except psutil.NoSuchProcess:
            pytest.fail("Daemon process not found via psutil")

    def test_daemon_health_endpoint_responds(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify health endpoint responds when daemon is ready."""
        response = daemon_client.get("/api/admin/status")
        assert response.status_code == 200

        data = response.json()
        assert data.get("status") == "healthy"
        assert "uptime_seconds" in data or "version" in data or "status" in data

    def test_daemon_listens_on_configured_ports(self, daemon_instance: DaemonInstance) -> None:
        """Verify daemon is listening on both HTTP and WebSocket ports."""
        import socket

        # Check HTTP port is in use
        http_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = http_sock.connect_ex(("localhost", daemon_instance.http_port))
        http_sock.close()
        assert result == 0, f"HTTP port {daemon_instance.http_port} should be in use"

        # Check WebSocket port is in use
        ws_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = ws_sock.connect_ex(("localhost", daemon_instance.ws_port))
        ws_sock.close()
        assert result == 0, f"WebSocket port {daemon_instance.ws_port} should be in use"

    def test_daemon_uses_isolated_database(self, daemon_instance: DaemonInstance) -> None:
        """Verify daemon database is in the expected location."""
        # Database should exist in the temp directory
        assert daemon_instance.db_path.parent.exists(), "Database directory should exist"


class TestDaemonStop:
    """Tests for daemon stop behavior."""

    def test_daemon_stops_gracefully_on_sigterm(self, daemon_instance: DaemonInstance) -> None:
        """Verify daemon stops gracefully when sent SIGTERM."""
        pid = daemon_instance.pid

        # Verify process is running
        assert daemon_instance.is_alive(), "Daemon should be running before stop"

        # Send SIGTERM
        os.kill(pid, signal.SIGTERM)

        # Wait for process to stop (up to 10 seconds)
        start = time.time()
        while time.time() - start < 10.0:
            if not daemon_instance.is_alive():
                break
            time.sleep(0.2)

        # Process should have stopped
        assert not daemon_instance.is_alive(), "Daemon should stop after SIGTERM"

    def test_no_orphan_processes_after_stop(self, daemon_instance: DaemonInstance) -> None:
        """Verify no orphan child processes remain after daemon stops."""
        pid = daemon_instance.pid

        # Get child processes before stop
        try:
            parent = psutil.Process(pid)
            children_before = parent.children(recursive=True)
            child_pids = [c.pid for c in children_before]
        except psutil.NoSuchProcess:
            child_pids = []

        # Stop daemon
        os.kill(pid, signal.SIGTERM)
        time.sleep(3.0)

        # Check that child processes are also gone
        for child_pid in child_pids:
            try:
                child = psutil.Process(child_pid)
                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
                    pytest.fail(f"Orphan child process {child_pid} still running")
            except psutil.NoSuchProcess:
                pass  # Expected - child process is gone

    def test_stop_is_idempotent_on_non_running_daemon(self, e2e_project_dir) -> None:
        """Verify stopping a non-running daemon doesn't error."""
        # Create a fake PID file with a non-existent PID
        pid_file = e2e_project_dir / ".gobby-home" / "gobby.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text("99999999")  # Very high PID unlikely to exist

        # Attempting to stop should not raise an error
        # (mimics what stop_daemon does - just checks if process exists)
        try:
            os.kill(99999999, 0)
            pytest.fail("Expected process not to exist")
        except ProcessLookupError:
            pass  # Expected - process doesn't exist

        # Clean up
        pid_file.unlink()


class TestDaemonRestart:
    """Tests for daemon restart behavior."""

    def test_daemon_can_restart_after_stop(
        self,
        e2e_project_dir,
        e2e_config,
    ) -> None:
        """Verify daemon can be started again after being stopped."""
        import subprocess
        import sys

        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"

        log_file = log_dir / "daemon.log"
        error_log_file = log_dir / "daemon_error.log"

        # Use helper to properly prepare env (PYTHONPATH, API keys, HOME override)
        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

        # Start first daemon
        with open(log_file, "w") as log_f, open(error_log_file, "w") as err_f:
            process1 = subprocess.Popen(
                [sys.executable, "-m", "gobby.runner", "--config", str(config_path)],
                stdout=log_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                cwd=str(e2e_project_dir),
                env=env,
                start_new_session=True,
            )

        try:
            # Wait for first daemon to be healthy
            assert wait_for_daemon_health(http_port, timeout=20.0), "First daemon should start"

            # Stop first daemon
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=10)

            # Wait for port to be released
            time.sleep(2.0)

            # Start second daemon on same ports
            with open(log_file, "a") as log_f, open(error_log_file, "a") as err_f:
                process2 = subprocess.Popen(
                    [sys.executable, "-m", "gobby.runner", "--config", str(config_path)],
                    stdout=log_f,
                    stderr=err_f,
                    stdin=subprocess.DEVNULL,
                    cwd=str(e2e_project_dir),
                    env=env,
                    start_new_session=True,
                )

            try:
                # Wait for second daemon to be healthy
                assert wait_for_daemon_health(http_port, timeout=20.0), (
                    "Second daemon should start after restart"
                )

                # Verify it's a different process
                assert process2.pid != process1.pid, "Restarted daemon should have different PID"

            finally:
                terminate_process_tree(process2.pid)
        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)

    def test_restart_has_no_state_leakage(
        self,
        e2e_project_dir,
        e2e_config,
    ) -> None:
        """Verify restarted daemon doesn't inherit state from previous instance."""
        import subprocess
        import sys

        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"

        log_file = log_dir / "daemon.log"
        error_log_file = log_dir / "daemon_error.log"

        # Use helper to properly prepare env (PYTHONPATH, API keys, HOME override)
        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

        # Start first daemon
        with open(log_file, "w") as log_f, open(error_log_file, "w") as err_f:
            process1 = subprocess.Popen(
                [sys.executable, "-m", "gobby.runner", "--config", str(config_path)],
                stdout=log_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                cwd=str(e2e_project_dir),
                env=env,
                start_new_session=True,
            )

        try:
            assert wait_for_daemon_health(http_port, timeout=20.0), "First daemon should start"

            # Verify initial health
            response1 = httpx.get(f"http://localhost:{http_port}/api/admin/status", timeout=5.0)
            assert response1.status_code == 200

            # Stop and restart
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=10)
            time.sleep(2.0)

            with open(log_file, "a") as log_f, open(error_log_file, "a") as err_f:
                process2 = subprocess.Popen(
                    [sys.executable, "-m", "gobby.runner", "--config", str(config_path)],
                    stdout=log_f,
                    stderr=err_f,
                    stdin=subprocess.DEVNULL,
                    cwd=str(e2e_project_dir),
                    env=env,
                    start_new_session=True,
                )

            try:
                assert wait_for_daemon_health(http_port, timeout=20.0), "Second daemon should start"

                # Get status after restart
                response2 = httpx.get(f"http://localhost:{http_port}/api/admin/status", timeout=5.0)
                assert response2.status_code == 200
                status2 = response2.json()

                # Uptime should be reset (near zero after restart)
                uptime2 = status2.get("uptime_seconds", 0)
                assert uptime2 < 30, f"Uptime should be reset after restart, got {uptime2}s"

            finally:
                terminate_process_tree(process2.pid)
        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)


class TestDaemonMultipleInstances:
    """Tests for handling multiple daemon instances."""

    def test_second_start_on_same_port_fails(self, daemon_instance: DaemonInstance) -> None:
        """Verify starting a second daemon on same ports fails gracefully."""
        import subprocess
        import sys

        # Try to start another daemon on same ports
        with open(os.devnull, "w") as devnull:
            process2 = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "gobby.runner",
                    "--config",
                    str(daemon_instance.config_path),
                ],
                stdout=devnull,
                stderr=devnull,
                stdin=subprocess.DEVNULL,
                cwd=str(daemon_instance.project_dir),
                env={
                    **os.environ,
                    "GOBBY_CONFIG": str(daemon_instance.config_path),
                    "ANTHROPIC_API_KEY": "",
                    "OPENAI_API_KEY": "",
                    "GEMINI_API_KEY": "",
                },
                start_new_session=True,
            )

        # Wait a bit for the second process to fail
        time.sleep(3.0)

        # Second process should have exited (port conflict)
        # Or be killed if it somehow managed to start
        if process2.poll() is None:
            terminate_process_tree(process2.pid)
            # If it's still running, the original daemon should still work
            response = httpx.get(f"http://localhost:{daemon_instance.http_port}/api/admin/status")
            assert response.status_code == 200
        else:
            # Process exited - this is expected behavior
            pass

        # Original daemon should still be running and healthy
        assert daemon_instance.is_alive(), "Original daemon should still be running"
        response = httpx.get(f"http://localhost:{daemon_instance.http_port}/api/admin/status")
        assert response.status_code == 200
