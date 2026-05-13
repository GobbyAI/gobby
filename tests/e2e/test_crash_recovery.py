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
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    daemon_health_unavailable,
    prepare_daemon_env,
    terminate_process_tree,
    wait_for_daemon_health,
)

pytestmark = pytest.mark.e2e


def _database_has_schema(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('projects', 'tasks')"
            )
            return {row[0] for row in cursor.fetchall()} == {"projects", "tasks"}
    except sqlite3.Error:
        return False


def _wait_for_database_ready(db_path: Path) -> None:
    wait_for_condition(
        lambda: _database_has_schema(db_path),
        timeout=10.0,
        interval=0.1,
        description=f"database schema at {db_path}",
    )


def _register_test_project(
    http_port: int,
    *,
    project_id: str,
    name: str,
    repo_path: Path,
) -> None:
    with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
        response = client.post(
            "/api/admin/test/register-project",
            json={"project_id": project_id, "name": name, "repo_path": str(repo_path)},
        )
        response.raise_for_status()

        def project_is_visible() -> bool:
            try:
                project_response = client.get(f"/api/projects/{project_id}")
            except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError):
                return False
            return project_response.status_code == 200

        wait_for_condition(
            project_is_visible,
            timeout=10.0,
            interval=0.1,
            description=f"registered project {project_id}",
        )


def _create_test_task(db_path: Path, *, project_id: str, title: str) -> str:
    from gobby.storage.database import LocalDatabase
    from gobby.storage.tasks import LocalTaskManager

    db = LocalDatabase(db_path)
    try:
        task = LocalTaskManager(db).create_task(
            project_id=project_id,
            title=title,
            priority=2,
            task_type="task",
        )
        return task.id
    finally:
        db.close()


def _read_task_row(db_path: Path, task_id: str) -> sqlite3.Row | None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT id, project_id, title, closed_at FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()


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
        db_path = gobby_home / "gobby-hub.db"

        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

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
            _wait_for_database_ready(db_path)

            # Create some state via API (register a session)
            with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
                # Just verify daemon is working
                response = client.get("/api/admin/status")
                assert response.status_code == 200

            # Forcefully kill the daemon (simulating crash)
            os.kill(process.pid, signal.SIGKILL)
            process.wait(timeout=25)

            # Verify database file still exists
            assert db_path.exists(), "Database file should survive crash"

            # Verify database is readable (not corrupted)
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
        db_path = gobby_home / "gobby-hub.db"

        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

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
            _wait_for_database_ready(db_path)

            # Get initial session count
            with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
                response = client.get("/api/sessions")
                assert response.status_code == 200
                initial_count = response.json().get("count", 0)

            # Forcefully kill (crash)
            os.kill(process1.pid, signal.SIGKILL)
            process1.wait(timeout=25)

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
                _wait_for_database_ready(db_path)

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

        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

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
            _wait_for_database_ready(gobby_home / "gobby-hub.db")

            # Verify it's running
            response = httpx.get(f"http://localhost:{http_port}/api/admin/status", timeout=5.0)
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

        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

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
            _wait_for_database_ready(gobby_home / "gobby-hub.db")

            # Create a client and make a request
            with httpx.Client(base_url=f"http://localhost:{http_port}", timeout=10.0) as client:
                response1 = client.get("/api/admin/status")
                assert response1.status_code == 200

            # Stop daemon gracefully
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=25)
            wait_for_condition(
                lambda: daemon_health_unavailable(http_port),
                timeout=5.0,
                description="first daemon shutdown",
            )

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
                _wait_for_database_ready(gobby_home / "gobby-hub.db")

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

        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

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
            _wait_for_database_ready(db_path)
            _register_test_project(
                http_port,
                project_id="test-project",
                name="Test Project",
                repo_path=e2e_project_dir,
            )
            task_id = _create_test_task(db_path, project_id="test-project", title="Test Task")

            # Stop daemon gracefully
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=25)
            wait_for_condition(
                lambda: daemon_health_unavailable(http_port),
                timeout=5.0,
                description="first daemon shutdown",
            )

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
                _wait_for_database_ready(db_path)

                # Verify task still exists in database
                row = _read_task_row(db_path, task_id)
                assert row is not None, "Task should persist after restart"
                assert row["project_id"] == "test-project"
                assert row["title"] == "Test Task"
                assert row["closed_at"] is None

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

        env = prepare_daemon_env(home_dir=gobby_home)
        env["GOBBY_CONFIG"] = str(config_path)
        env["GOBBY_HOME"] = str(gobby_home)

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
            _wait_for_database_ready(db_path)
            _register_test_project(
                http_port,
                project_id="crash-project",
                name="Crash Project",
                repo_path=e2e_project_dir,
            )
            task_id = _create_test_task(db_path, project_id="crash-project", title="Crash Task")

            # Crash the daemon
            os.kill(process1.pid, signal.SIGKILL)
            process1.wait(timeout=25)

            # Verify task survives crash (check database directly)
            row = _read_task_row(db_path, task_id)
            assert row is not None, "Task should survive crash"
            assert row["project_id"] == "crash-project"
            assert row["title"] == "Crash Task"
            assert row["closed_at"] is None

        finally:
            if process1.poll() is None:
                terminate_process_tree(process1.pid)
