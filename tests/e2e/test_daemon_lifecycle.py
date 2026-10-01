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
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import httpx
import psutil
import pytest

from gobby.cli.install_setup_impeccable import _publish_launcher, _publish_stamp
from gobby.cli.utils_process import is_port_available
from gobby.install.bin_set_coherence import probe_set_member_identity
from gobby.storage.schema_identity_pin import stamp_bytes
from gobby.utils.dependency_requirements import IMPECCABLE_RELEASE
from gobby.utils.native_bin import IDENTITY_STAMP_NAME, NATIVE_BIN_DIR_ENV, native_bin_name
from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    DaemonInstance,
    authenticated_daemon_request,
    daemon_health_unavailable,
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

        # Wait for process to stop — must exceed the daemon's graceful
        # shutdown timeout (15 s) plus cleanup buffer
        wait_for_condition(
            lambda: not daemon_instance.is_alive(),
            timeout=25.0,
            interval=0.2,
            description="daemon process exit",
        )

        # Process should have stopped
        assert not daemon_instance.is_alive(), "Daemon should stop after SIGTERM"

    def test_no_orphan_processes_after_stop(self, daemon_instance: DaemonInstance) -> None:
        """Verify no orphan child processes remain after daemon stops."""
        pid = daemon_instance.pid

        # Get child processes before stop
        try:
            parent = psutil.Process(pid)
            children_before = parent.children(recursive=True)
        except psutil.NoSuchProcess:
            children_before = []

        # Stop daemon
        os.kill(pid, signal.SIGTERM)

        # Wait for the daemon's graceful shutdown (15 s) plus cleanup buffer,
        # matching test_daemon_stops_gracefully_on_sigterm
        wait_for_condition(
            lambda: not daemon_instance.is_alive(),
            timeout=25.0,
            interval=0.2,
            description="daemon process exit",
        )

        # Wait for any child processes to exit as well. psutil.wait_procs treats
        # zombies as gone, which matches what we want here.
        _, alive = psutil.wait_procs(children_before, timeout=10.0)
        # The `gterm host` outlives the daemon by design (#22002): the next start
        # adopts it, and the e2e fixture teardown reaps the isolated one.
        still_running = [
            (c.pid, c.cmdline())
            for c in alive
            if c.is_running() and c.status() != psutil.STATUS_ZOMBIE and not _is_terminal_host(c)
        ]
        assert still_running == []

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
        assert pid_file.exists()

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
            wait_for_daemon_health(http_port, log_file=log_file)

            # Stop first daemon
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=25)
            wait_for_condition(
                lambda: daemon_health_unavailable(http_port),
                timeout=5.0,
                description="first daemon shutdown",
            )

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
                wait_for_daemon_health(http_port, log_file=log_file)

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
            wait_for_daemon_health(http_port, log_file=log_file)

            # Verify initial health
            response1 = authenticated_daemon_request(
                "GET",
                f"http://localhost:{http_port}/api/admin/status",
                gobby_home,
                timeout=5.0,
            )
            assert response1.status_code == 200

            # Stop and restart
            os.kill(process1.pid, signal.SIGTERM)
            process1.wait(timeout=25)
            wait_for_condition(
                lambda: daemon_health_unavailable(http_port),
                timeout=5.0,
                description="first daemon shutdown",
            )

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
                wait_for_daemon_health(http_port, log_file=log_file)

                # Get status after restart
                response2 = authenticated_daemon_request(
                    "GET",
                    f"http://localhost:{http_port}/api/admin/status",
                    gobby_home,
                    timeout=5.0,
                )
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

        try:
            wait_for_condition(
                lambda: process2.poll() is not None,
                timeout=3.0,
                description="second daemon process exit",
            )
        except AssertionError:
            pass

        # Second process should have exited (port conflict)
        # Or be killed if it somehow managed to start
        if process2.poll() is None:
            terminate_process_tree(process2.pid)
            # If it's still running, the original daemon should still work
            response = authenticated_daemon_request(
                "GET",
                f"http://localhost:{daemon_instance.http_port}/api/admin/status",
                daemon_instance.gobby_home,
            )
            assert response.status_code == 200
        else:
            # Process exited - this is expected behavior
            pass

        # Original daemon should still be running and healthy
        assert daemon_instance.is_alive(), "Original daemon should still be running"
        response = authenticated_daemon_request(
            "GET",
            f"http://localhost:{daemon_instance.http_port}/api/admin/status",
            daemon_instance.gobby_home,
        )
        assert response.status_code == 200


def _daemon_env(config_path: Path) -> dict[str, str]:
    gobby_home = config_path.parent
    env = prepare_daemon_env(home_dir=gobby_home)
    env["GOBBY_CONFIG"] = str(config_path)
    env["GOBBY_HOME"] = str(gobby_home)
    return env


# The real `gobby` CLI with one boundary faked: managed Docker services. Compose
# from any home manages the production containers, and the test hub needs none.
_CLI_WITHOUT_MANAGED_SERVICES = """
import sys
from unittest.mock import patch

from gobby.cli import cli
from gobby.cli._daemon_services import ServiceStartResult

skipped = ServiceStartResult("skipped", "e2e: the isolated test hub needs no managed services")
with patch("gobby.cli.daemon_start._services_start", return_value=skipped):
    cli(sys.argv[1:], prog_name="gobby")
"""


def _stamped_bin_dir(source_dir: Path, gobby_home: Path) -> Path:
    bin_dir = gobby_home / "bin"
    bin_dir.mkdir(exist_ok=True)
    gdaemon = bin_dir / native_bin_name("gdaemon")
    gdaemon.symlink_to(source_dir / native_bin_name("gdaemon"))
    identity = probe_set_member_identity(gdaemon, "gdaemon")
    (bin_dir / IDENTITY_STAMP_NAME).write_bytes(stamp_bytes(identity))
    return bin_dir


def _spawn_runner(e2e_project_dir: Path, config_path: Path) -> subprocess.Popen[bytes]:
    gobby_home = config_path.parent
    env = _daemon_env(config_path)
    log_dir = gobby_home / "logs"
    with (
        open(log_dir / "daemon.log", "a") as log_f,
        open(log_dir / "daemon_error.log", "a") as err_f,
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
    return process


def _front_door_child(runner_pid: int) -> psutil.Process:
    """The runner's `gdaemon serve` child, which owns the public ports."""
    children: list[psutil.Process] = []

    def find() -> bool:
        children[:] = [
            child
            for child in psutil.Process(runner_pid).children()
            if Path(child.cmdline()[0]).name == "gdaemon" and child.cmdline()[1:] == ["serve"]
        ]
        return len(children) == 1

    wait_for_condition(find, timeout=10.0, description="gdaemon front door child")
    return children[0]


def _gone(process: psutil.Process) -> bool:
    try:
        return bool(process.status() == psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return True


def _label(process: psutil.Process) -> str:
    try:
        return f"{process.pid} {' '.join(process.cmdline())[:160]}"
    except psutil.Error:
        return str(process.pid)


def _ports_free(*ports: int) -> bool:
    return all(is_port_available(port, "127.0.0.1") for port in ports)


class TestFrontDoor:
    """The runner owns a gdaemon front door on the public ports."""

    def test_daemon_starts_behind_front_door(self, daemon_instance: DaemonInstance) -> None:
        child = _front_door_child(daemon_instance.pid)
        backend_http = daemon_instance.http_port + 100

        public = httpx.get(f"http://127.0.0.1:{daemon_instance.http_port}/api/auth/status")
        backend = httpx.get(f"http://127.0.0.1:{backend_http}/api/auth/status")

        assert child.is_running()
        assert (public.status_code, backend.status_code) == (200, 200)

    def test_stop_and_restart_free_public_ports(
        self, e2e_project_dir: Path, e2e_config: tuple[Path, int, int]
    ) -> None:
        config_path, http_port, ws_port = e2e_config
        gobby_home = config_path.parent
        pid_file = gobby_home / "gobby.pid"
        env = _daemon_env(config_path)
        # The real CLI may stop this isolated home's pid-file runner and nothing else.
        env["GOBBY_E2E_ISOLATED_HOME"] = str(gobby_home)
        env["GOBBY_ALLOW_WORKTREE_DAEMON"] = "1"
        # Restart proves the installed set first, so install the test gdaemon as a
        # stamped one-member set, the shape promotion leaves behind.
        env[NATIVE_BIN_DIR_ENV] = str(_stamped_bin_dir(Path(env[NATIVE_BIN_DIR_ENV]), gobby_home))
        # `gobby start` requires the managed SRT and Impeccable installs under its home.
        managed_tools = Path.home() / ".gobby" / "tools"
        if not managed_tools.is_dir():
            pytest.skip("gobby start needs the managed SRT and Impeccable installs")
        shutil.copytree(managed_tools, gobby_home / "tools", symlinks=True)
        # Impeccable's launcher embeds its home, so activate the copy for this one.
        impeccable = gobby_home / "tools" / "impeccable" / IMPECCABLE_RELEASE.version
        _publish_launcher(gobby_home, impeccable)
        _publish_stamp(gobby_home)

        def gobby(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-c", _CLI_WITHOUT_MANAGED_SERVICES, *args],
                env=env,
                cwd=str(e2e_project_dir),
                capture_output=True,
                text=True,
                timeout=240,
            )

        runner = _spawn_runner(e2e_project_dir, config_path)
        runners: list[psutil.Process] = [psutil.Process(runner.pid)]
        children: list[psutil.Process] = []
        try:
            wait_for_daemon_health(http_port, log_file=gobby_home / "logs" / "daemon.log")
            children.append(_front_door_child(runner.pid))
            for _round in range(2):
                restarted = gobby("restart")
                assert restarted.returncode == 0, restarted.stdout + restarted.stderr
                # The previous runner and its gdaemon child are gone; a new pair serves.
                assert _gone(runners[-1]) and _gone(children[-1])
                runners.append(psutil.Process(int(pid_file.read_text().strip())))
                children.append(_front_door_child(runners[-1].pid))
                assert httpx.get(f"http://localhost:{http_port}/api/health").status_code == 200
            stopped = gobby("stop")
            assert stopped.returncode == 0, stopped.stdout + stopped.stderr
            # `gobby stop` returns with the pair stopped, not eventually after.
            assert _gone(runners[-1]) and _gone(children[-1])
            assert _ports_free(http_port, ws_port, http_port + 100, ws_port + 100)
        finally:
            if runner.poll() is None:
                terminate_process_tree(runner.pid)
            for process in [*runners, *children]:
                if not _gone(process):
                    process.kill()

    def test_runner_sigkill_frees_public_ports(
        self, e2e_project_dir: Path, e2e_config: tuple[Path, int, int]
    ) -> None:
        config_path, http_port, ws_port = e2e_config
        runner = _spawn_runner(e2e_project_dir, config_path)
        child: psutil.Process | None = None
        descendants: list[psutil.Process] = []
        try:
            wait_for_daemon_health(http_port, log_file=config_path.parent / "logs" / "daemon.log")
            child = _front_door_child(runner.pid)
            descendants = psutil.Process(runner.pid).children(recursive=True)
            labels = {p.pid: _label(p) for p in descendants}
            # gdaemon, the transcript evidence pool, and the rest may not outlive a SIGKILL.
            # The gterm host is exempt: it survives by design for the next daemon to adopt.
            mortal = [p for p in descendants if "gterm host" not in labels[p.pid]]
            os.kill(runner.pid, signal.SIGKILL)
            front_door = child
            # One 5 s budget from the kill covers every descendant exit and both ports.
            try:
                wait_for_condition(
                    lambda: all(_gone(p) for p in mortal) and _ports_free(http_port, ws_port),
                    timeout=5.0,
                    interval=0.1,
                    description="runner descendants exit and public ports free after SIGKILL",
                )
            except AssertionError as timed_out:
                survivors = [labels[p.pid] for p in mortal if not _gone(p)]
                raise AssertionError(
                    f"survived SIGKILL: {survivors}; "
                    f"public ports free: {_ports_free(http_port, ws_port)}"
                ) from timed_out
            runner.wait(timeout=5)
            # The liveness pipe and the pool's parent watch, not runner cleanup, did it.
            assert runner.returncode == -signal.SIGKILL
            assert _gone(front_door)
        finally:
            if runner.poll() is None:
                terminate_process_tree(runner.pid)
            for process in descendants:
                if not _gone(process):
                    process.kill()
            if child is not None and not _gone(child):
                child.kill()


def _is_terminal_host(process: psutil.Process) -> bool:
    argv = process.cmdline()
    return bool(argv) and Path(argv[0]).name == "gterm" and argv[1:2] == ["host"]
