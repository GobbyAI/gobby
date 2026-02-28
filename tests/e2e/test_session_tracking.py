"""
E2E tests for session tracking across CLI events.

Tests verify:
1. Sessions endpoint is accessible and returns data
2. Hook events can be executed
3. Session state is tracked
4. Multiple operations don't corrupt session data
"""

import os
import signal
import subprocess
import sys
import time
import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    prepare_daemon_env,
    terminate_process_tree,
    wait_for_daemon_health,
)

pytestmark = pytest.mark.e2e


class TestSessionEndpoint:
    """Tests for sessions endpoint functionality."""

    def test_sessions_endpoint_returns_data(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify sessions endpoint returns session data."""
        response = daemon_client.get("/api/sessions")
        assert response.status_code == 200

        data = response.json()
        assert "sessions" in data
        assert "count" in data
        assert isinstance(data["sessions"], list)
        assert isinstance(data["count"], int)

    def test_sessions_endpoint_supports_filtering(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify sessions endpoint supports query parameters."""
        # Test with limit parameter
        response = daemon_client.get("/api/sessions", params={"limit": 10})
        assert response.status_code == 200

        data = response.json()
        assert "sessions" in data

    def test_sessions_response_time_is_included(
        self, daemon_instance: DaemonInstance, daemon_client: httpx.Client
    ) -> None:
        """Verify sessions endpoint includes response time metrics."""
        response = daemon_client.get("/api/sessions")
        assert response.status_code == 200

        data = response.json()
        assert "response_time_ms" in data


class TestHookEvents:
    """Tests for hook event execution."""

    def test_session_start_hook_executes(
        self, daemon_instance: DaemonInstance, cli_events: CLIEventSimulator
    ) -> None:
        """Verify session start hook can be executed."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"

        result = cli_events.session_start(
            session_id=session_id,
            machine_id="test-machine",
            source="claude",
        )

        # Hook should execute and return a decision
        assert "continue" in result or "decision" in result

    def test_session_end_hook_executes(
        self, daemon_instance: DaemonInstance, cli_events: CLIEventSimulator
    ) -> None:
        """Verify session end hook can be executed."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"

        # Start then end session
        cli_events.session_start(
            session_id=session_id,
            machine_id="test-machine",
            source="claude",
        )

        result = cli_events.session_end(
            session_id=session_id,
            machine_id="test-machine",
            source="claude",
        )

        # Hook should execute
        assert result is not None

    def test_tool_use_hook_executes(
        self, daemon_instance: DaemonInstance, cli_events: CLIEventSimulator
    ) -> None:
        """Verify tool use hook can be executed."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"

        # Start session first
        cli_events.session_start(
            session_id=session_id,
            machine_id="test-machine",
            source="claude",
        )

        result = cli_events.tool_use(
            session_id=session_id,
            tool_name="Read",
            tool_input={"file_path": "/tmp/test.txt"},
            source="claude",
        )

        # Hook should execute
        assert result is not None


class TestSessionState:
    """Tests for session state management."""

    def test_sessions_endpoint_accessible_after_hooks(
        self, daemon_instance: DaemonInstance, cli_events: CLIEventSimulator
    ) -> None:
        """Verify sessions endpoint works after hook events."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"

        # Execute some hooks
        cli_events.session_start(
            session_id=session_id,
            machine_id="test-machine",
            source="claude",
        )

        # Sessions endpoint should still work
        response = cli_events.client.get("/api/sessions")
        assert response.status_code == 200

        data = response.json()
        assert "sessions" in data

    def test_multiple_hook_events_dont_corrupt_state(
        self, daemon_instance: DaemonInstance, cli_events: CLIEventSimulator
    ) -> None:
        """Verify multiple hook events don't corrupt session state."""
        # Execute multiple session hooks
        for i in range(3):
            session_id = f"test-session-{i}-{uuid.uuid4().hex[:8]}"
            cli_events.session_start(
                session_id=session_id,
                machine_id="test-machine",
                source="claude",
            )

        # Sessions endpoint should still work
        response = cli_events.client.get("/api/sessions")
        assert response.status_code == 200

        data = response.json()
        assert "sessions" in data
        assert "count" in data


class TestSessionPersistence:
    """Tests for session persistence across daemon restarts."""

    def test_sessions_endpoint_works_after_restart(
        self,
        e2e_project_dir,
        e2e_config,
    ) -> None:
        """Verify sessions endpoint works after daemon restart."""
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
            assert wait_for_daemon_health(http_port, timeout=20.0), "Daemon should start"

            # Verify sessions endpoint works
            client = httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0)
            try:
                response = client.get("/api/sessions")
                assert response.status_code == 200
            finally:
                client.close()

            # Stop daemon gracefully
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=10)
            time.sleep(2.0)

            # Start second daemon
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
                assert wait_for_daemon_health(http_port, timeout=20.0), "Daemon should restart"

                # Verify sessions endpoint still works
                client = httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0)
                try:
                    response = client.get("/api/sessions")
                    assert response.status_code == 200

                    data = response.json()
                    assert "sessions" in data
                finally:
                    client.close()

            finally:
                terminate_process_tree(process2.pid)
        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)
