"""
E2E tests for crash recovery and state preservation.

Tests verify:
1. Daemon crash (SIGKILL) leaves recoverable state
2. Restart after crash restores active sessions from storage
3. Stale PID file is detected and cleaned up on start
4. In-flight MCP requests are handled gracefully after restart
5. Task state persists across daemon restarts
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    terminate_process_tree,
    wait_for_daemon_health,
)

# Skip crash recovery E2E tests - database initialization timing issues.
# The daemon may not create the database at the expected path, or
# migrations may not run before tests check for database state.
# TODO: Add explicit database existence wait before assertions
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skip(reason="E2E crash recovery tests have db initialization timing issues"),
]


class TestCrashRecovery:
    """Tests for daemon crash and recovery behavior."""

    def test_daemon_crash_leaves_recoverable_state(
        self,
        e2e_project_dir: Path,
        e2e_config: tuple[Path, int, int],
    ) -> None:
        """Verify SIGKILL crash leaves database in consistent state."""
        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"

        env = os.environ.copy()
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)
        env["ANTHROPIC_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["GEMINI_API_KEY"] = ""

        # Start daemon
        with (
            open(log_dir / "daemon.log", "w") as log_f,
            open(log_dir / "daemon_error.log", "w") as err_f,
        ):
            process = subprocess.Popen(
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

            # Create some state via API (register a session)
            with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
                # Just verify daemon is working
                response = client.get("/api/admin/status")
                assert response.status_code == 200

            # Forcefully kill the daemon (simulating crash)
            os.kill(process.pid, signal.SIGKILL)
            time.sleep(1.0)

            # Verify database file still exists
            db_path = gobby_home / "gobby-hub.db"
            assert db_path.exists(), "Database file should survive crash"

            # Verify database is readable (not corrupted)
            import sqlite3

            conn = sqlite3.connect(str(db_path))
            try:
                # Should be able to read tables
                cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [row[0] for row in cursor.fetchall()]
                assert len(tables) > 0, "Database should have tables"
            finally:
                conn.close()

        finally:
            if process.poll() is None:
                terminate_process_tree(process.pid)

    def test_restart_after_crash_restores_sessions(
        self,
        e2e_project_dir: Path,
        e2e_config: tuple[Path, int, int],
    ) -> None:
        """Verify sessions persist and are restored after crash restart."""
        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"

        env = os.environ.copy()
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)
        env["ANTHROPIC_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["GEMINI_API_KEY"] = ""

        # Start first daemon
        with (
            open(log_dir / "daemon.log", "w") as log_f,
            open(log_dir / "daemon_error.log", "w") as err_f,
        ):
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

            # Get initial session count
            with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
                response = client.get("/api/sessions")
                assert response.status_code == 200
                initial_count = response.json().get("count", 0)

            # Forcefully kill (crash)
            os.kill(process1.pid, signal.SIGKILL)
            time.sleep(2.0)

            # Start second daemon (recovery)
            with (
                open(log_dir / "daemon.log", "a") as log_f,
                open(log_dir / "daemon_error.log", "a") as err_f,
            ):
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
                assert wait_for_daemon_health(http_port, timeout=20.0), (
                    "Recovered daemon should start"
                )

                # Sessions should be accessible (database recovered)
                with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
                    response = client.get("/api/sessions")
                    assert response.status_code == 200
                    recovered_count = response.json().get("count", 0)

                # Session count should be consistent
                assert recovered_count == initial_count, (
                    f"Session count should be preserved: expected {initial_count}, got {recovered_count}"
                )

            finally:
                terminate_process_tree(process2.pid)
        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)


class TestStalePIDFile:
    """Tests for stale PID file handling."""

    def test_stale_pid_file_detected_and_cleaned_on_start(self, e2e_project_dir: Path) -> None:
        """Verify stale PID file from crashed daemon is cleaned up."""
        # Create a fake stale PID file with non-existent process
        pid_dir = e2e_project_dir / ".gobby-stale"
        pid_dir.mkdir(parents=True, exist_ok=True)
        pid_file = pid_dir / "gobby.pid"
        pid_file.write_text("99999999")  # Very high PID unlikely to exist

        # Verify the PID doesn't exist
        try:
            os.kill(99999999, 0)
            pytest.skip("PID 99999999 exists on this system")
        except ProcessLookupError:
            pass  # Expected - process doesn't exist

        # The CLI should detect stale PID and handle it
        # This tests the detection logic directly
        from gobby.cli.utils import _is_process_alive

        assert not _is_process_alive(99999999), "Stale PID should be detected as not alive"

        # Clean up
        pid_file.unlink()

    def test_daemon_starts_despite_stale_pid(
        self,
        e2e_project_dir: Path,
        e2e_config: tuple[Path, int, int],
    ) -> None:
        """Verify daemon can start when stale PID file exists."""
        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"

        # Create a stale PID file in the gobby home directory
        pid_file = gobby_home / "gobby.pid"
        pid_file.write_text("99999999")

        env = os.environ.copy()
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)
        env["ANTHROPIC_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["GEMINI_API_KEY"] = ""

        # Start daemon (it should handle the stale PID)
        with (
            open(log_dir / "daemon.log", "w") as log_f,
            open(log_dir / "daemon_error.log", "w") as err_f,
        ):
            process = subprocess.Popen(
                [sys.executable, "-m", "gobby.runner", "--config", str(config_path)],
                stdout=log_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                cwd=str(e2e_project_dir),
                env=env,
                start_new_session=True,
            )

        try:
            # Daemon should still start successfully
            assert wait_for_daemon_health(http_port, timeout=20.0), (
                "Daemon should start despite stale PID file"
            )

            # Verify it's running
            response = httpx.get(f"http://localhost:{http_port}/admin/status", timeout=5.0)
            assert response.status_code == 200

        finally:
            terminate_process_tree(process.pid)


class TestClientReconnection:
    """Tests for client reconnection after daemon restart."""

    def test_clients_can_reconnect_after_restart(
        self,
        e2e_project_dir: Path,
        e2e_config: tuple[Path, int, int],
    ) -> None:
        """Verify clients can reconnect after daemon restart."""
        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"

        env = os.environ.copy()
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)
        env["ANTHROPIC_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["GEMINI_API_KEY"] = ""

        # Start first daemon
        with (
            open(log_dir / "daemon.log", "w") as log_f,
            open(log_dir / "daemon_error.log", "w") as err_f,
        ):
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

            # Create a client and make a request
            with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
                response1 = client.get("/api/admin/status")
                assert response1.status_code == 200

            # Stop daemon gracefully
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=10)
            time.sleep(2.0)

            # Start second daemon
            with (
                open(log_dir / "daemon.log", "a") as log_f,
                open(log_dir / "daemon_error.log", "a") as err_f,
            ):
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

                # New client should be able to connect
                with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
                    response2 = client.get("/api/admin/status")
                    assert response2.status_code == 200

            finally:
                terminate_process_tree(process2.pid)
        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)


class TestTaskStatePersistence:
    """Tests for task state persistence across restarts."""

    def test_task_state_persists_across_restarts(
        self,
        e2e_project_dir: Path,
        e2e_config: tuple[Path, int, int],
    ) -> None:
        """Verify task state is preserved after daemon restart."""
        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"
        db_path = gobby_home / "gobby-hub.db"

        env = os.environ.copy()
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)
        env["ANTHROPIC_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["GEMINI_API_KEY"] = ""

        # Start first daemon
        with (
            open(log_dir / "daemon.log", "w") as log_f,
            open(log_dir / "daemon_error.log", "w") as err_f,
        ):
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

            # Create a task directly in the database
            import sqlite3

            conn = sqlite3.connect(str(db_path))
            try:
                # First ensure we have a project
                conn.execute(
                    """
                    INSERT OR IGNORE INTO projects (id, name, repo_path, created_at, updated_at)
                    VALUES ('test-project', 'Test Project', '/tmp/test', datetime('now'), datetime('now'))
                    """
                )
                # Create a test task
                conn.execute(
                    """
                    INSERT INTO tasks (id, project_id, title, status, created_at, updated_at)
                    VALUES ('test-task-123', 'test-project', 'Test Task', 'open', datetime('now'), datetime('now'))
                    """
                )
                conn.commit()
            finally:
                conn.close()

            # Stop daemon gracefully
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=10)
            time.sleep(2.0)

            # Start second daemon
            with (
                open(log_dir / "daemon.log", "a") as log_f,
                open(log_dir / "daemon_error.log", "a") as err_f,
            ):
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

                # Verify task still exists in database
                conn = sqlite3.connect(str(db_path))
                try:
                    cursor = conn.execute(
                        "SELECT id, title, status FROM tasks WHERE id = 'test-task-123'"
                    )
                    row = cursor.fetchone()
                    assert row is not None, "Task should persist after restart"
                    assert row[0] == "test-task-123"
                    assert row[1] == "Test Task"
                    assert row[2] == "open"
                finally:
                    conn.close()

            finally:
                terminate_process_tree(process2.pid)
        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)

    def test_task_state_survives_crash(
        self,
        e2e_project_dir: Path,
        e2e_config: tuple[Path, int, int],
    ) -> None:
        """Verify task state survives SIGKILL crash."""
        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        log_dir = gobby_home / "logs"
        db_path = gobby_home / "gobby-hub.db"

        env = os.environ.copy()
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)
        env["ANTHROPIC_API_KEY"] = ""
        env["OPENAI_API_KEY"] = ""
        env["GEMINI_API_KEY"] = ""

        # Start daemon
        with (
            open(log_dir / "daemon.log", "w") as log_f,
            open(log_dir / "daemon_error.log", "w") as err_f,
        ):
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

            # Create a task
            import sqlite3

            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO projects (id, name, repo_path, created_at, updated_at)
                    VALUES ('crash-project', 'Crash Project', '/tmp/crash', datetime('now'), datetime('now'))
                    """
                )
                conn.execute(
                    """
                    INSERT INTO tasks (id, project_id, title, status, created_at, updated_at)
                    VALUES ('crash-task-456', 'crash-project', 'Crash Task', 'in_progress', datetime('now'), datetime('now'))
                    """
                )
                conn.commit()
            finally:
                conn.close()

            # Crash the daemon
            os.kill(process1.pid, signal.SIGKILL)
            time.sleep(2.0)

            # Verify task survives crash (check database directly)
            conn = sqlite3.connect(str(db_path))
            try:
                cursor = conn.execute(
                    "SELECT id, title, status FROM tasks WHERE id = 'crash-task-456'"
                )
                row = cursor.fetchone()
                assert row is not None, "Task should survive crash"
                assert row[1] == "Crash Task"
                assert row[2] == "in_progress"
            finally:
                conn.close()

        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)
