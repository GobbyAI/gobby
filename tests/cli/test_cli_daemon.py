"""Comprehensive tests for the CLI daemon module.

Tests the start, stop, restart, and status commands with various
argument combinations and error scenarios using Click's CliRunner.
"""

import os
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import psutil
import pytest
from click.testing import CliRunner

from gobby.agents.srt_runtime import SrtRuntimeError
from gobby.cli import cli
from gobby.cli.daemon import _reconcile_ui_exposure, _start_dependency_errors
from gobby.config.logging import RUNTIME_LOG_FILENAME, resolved_log_path
from gobby.ui_exposure import UiExposeError, UiExposeResult
from gobby.utils.status import RichStatusProbe

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _pin_boot_id() -> Generator[None]:
    """Keep the singleton claim off `sysctl`.

    These tests patch ``gobby.cli.daemon.subprocess.Popen`` — the process-wide
    stdlib attribute — so the boot-id probe inside ``claim_pid_file`` would run
    ``subprocess.run`` against the mock on macOS (#21033). Both importing
    modules bind the name, so both are patched.
    """
    with (
        patch("gobby.runner_pid_record.current_boot_id", return_value="boot:test"),
        patch("gobby.runner_pid_file.current_boot_id", return_value="boot:test"),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_protected_runs() -> Generator[MagicMock]:
    """Every stop path asks the daemon for restart-protected cron runs (#21021).

    Default to none so existing stop/restart tests never reach the network;
    protected-run tests reconfigure the yielded mock.
    """
    with patch("gobby.cli.daemon.fetch_protected_runs", return_value=[]) as fetch:
        yield fetch


@pytest.mark.parametrize("managed_services", [False, True])
def test_start_dependency_errors_detects_managed_services_from_home(
    tmp_path: Path,
    managed_services: bool,
) -> None:
    if managed_services:
        compose_file = tmp_path / "services" / "docker-compose.yml"
        compose_file.parent.mkdir()
        compose_file.touch()

    report = MagicMock()
    with (
        patch("gobby.cli.daemon.unsupported_platform_error", return_value=None),
        patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.daemon.collect_dependency_report", return_value=report) as collect,
        patch("gobby.cli.daemon.required_dependency_errors", return_value=[]) as required,
    ):
        result = _start_dependency_errors()

    assert result == []
    assert required.call_args.args == (report,)
    collect.assert_called_once_with(managed_services=managed_services, include_srt=True)
    required.assert_called_once_with(report)


@pytest.fixture(autouse=True)
def _mock_daemon_command_runtime(
    monkeypatch: pytest.MonkeyPatch,
    mock_daemon_config: MagicMock,
) -> None:
    from gobby.cli._daemon_services import ServiceStartResult

    monkeypatch.setattr(
        "gobby.cli.runtime.CliRuntime.require_database",
        lambda _runtime, **_kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        "gobby.cli.runtime.CliRuntime.require_config",
        lambda *_args, **_kwargs: mock_daemon_config,
    )
    monkeypatch.setattr("gobby.cli.daemon._start_dependency_errors", lambda: [])
    monkeypatch.setattr(
        "gobby.cli.daemon._services_start",
        lambda _home: ServiceStartResult("success", "Docker services started"),
    )


class TestDaemonHealthWait:
    """Tests for daemon health polling."""

    @pytest.mark.parametrize(
        "side_effect",
        [
            httpx.TimeoutException("timed out"),
            httpx.RequestError("request failed"),
        ],
    )
    @patch("gobby.cli.daemon.httpx.get")
    def test_is_daemon_healthy_returns_false_on_request_failures(
        self,
        mock_httpx_get: MagicMock,
        side_effect: Exception,
    ) -> None:
        """Timeouts and request failures both mean the daemon is unhealthy."""
        from gobby.cli.daemon import _is_daemon_healthy

        mock_httpx_get.side_effect = side_effect

        assert _is_daemon_healthy(60887) is False

    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.daemon.time.monotonic")
    @patch("gobby.cli.daemon.httpx.get")
    def test_wait_for_daemon_health_returns_elapsed_after_retry(
        self,
        mock_httpx_get: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Returns elapsed time once the health endpoint responds with 200."""
        from gobby.cli.daemon import _wait_for_daemon_health

        mock_httpx_get.side_effect = [
            httpx.ConnectError("daemon not ready"),
            MagicMock(status_code=200),
        ]
        mock_monotonic.side_effect = [0.0, 0.0, 0.5, 1.0]

        elapsed = _wait_for_daemon_health(60887, timeout=5.0, interval=0.5)

        assert elapsed == pytest.approx(1.0)
        assert mock_httpx_get.call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.daemon.time.monotonic")
    @patch("gobby.cli.daemon.httpx.get")
    def test_wait_for_daemon_health_treats_read_error_as_not_ready(
        self,
        mock_httpx_get: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Connection resets during restart health polling are transient."""
        from gobby.cli.daemon import _wait_for_daemon_health

        mock_httpx_get.side_effect = [
            httpx.ReadError("[Errno 54] Connection reset by peer"),
            MagicMock(status_code=200),
        ]
        mock_monotonic.side_effect = [0.0, 0.0, 0.5, 1.0]

        elapsed = _wait_for_daemon_health(60887, timeout=5.0, interval=0.5)

        assert elapsed == pytest.approx(1.0)
        assert mock_httpx_get.call_count == 2
        mock_sleep.assert_called_once_with(0.5)

    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.daemon.time.monotonic")
    @patch("gobby.cli.daemon.httpx.get")
    def test_wait_for_daemon_health_times_out(
        self,
        mock_httpx_get: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Returns None when the health endpoint never comes back."""
        from gobby.cli.daemon import _wait_for_daemon_health

        mock_httpx_get.side_effect = httpx.ConnectError("daemon not ready")
        mock_monotonic.side_effect = [0.0, 0.0, 1.0]

        elapsed = _wait_for_daemon_health(60887, timeout=1.0, interval=0.5)

        assert elapsed is None
        mock_sleep.assert_called_once_with(0.5)

    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.daemon.time.monotonic")
    @patch("gobby.cli.daemon.httpx.get")
    def test_wait_for_daemon_unhealthy_returns_elapsed_after_retry(
        self,
        mock_httpx_get: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Returns elapsed time once the health endpoint stops responding successfully."""
        from gobby.cli.daemon import _wait_for_daemon_unhealthy

        mock_httpx_get.side_effect = [
            MagicMock(status_code=200),
            httpx.ConnectError("daemon stopping"),
        ]
        mock_monotonic.side_effect = [0.0, 0.0, 0.25, 0.5]

        elapsed = _wait_for_daemon_unhealthy(60887, timeout=5.0, interval=0.25)

        assert elapsed == pytest.approx(0.5)
        assert mock_httpx_get.call_count == 2
        mock_sleep.assert_called_once_with(0.25)

    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.daemon.time.monotonic")
    @patch("gobby.cli.daemon.httpx.get")
    def test_wait_for_daemon_unhealthy_treats_read_error_as_stopped(
        self,
        mock_httpx_get: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Connection resets during stop polling mean the old daemon is no longer healthy."""
        from gobby.cli.daemon import _wait_for_daemon_unhealthy

        mock_httpx_get.side_effect = [
            MagicMock(status_code=200),
            httpx.ReadError("[Errno 54] Connection reset by peer"),
        ]
        mock_monotonic.side_effect = [0.0, 0.0, 0.25, 0.5]

        elapsed = _wait_for_daemon_unhealthy(60887, timeout=5.0, interval=0.25)

        assert elapsed == pytest.approx(0.5)
        assert mock_httpx_get.call_count == 2
        mock_sleep.assert_called_once_with(0.25)

    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.daemon.time.monotonic")
    @patch("gobby.cli.daemon._is_daemon_healthy")
    @patch("gobby.cli.daemon.get_service_status")
    @patch("gobby.cli.daemon._is_process_alive")
    def test_wait_for_service_stop_requires_pid_service_and_health_to_clear(
        self,
        mock_is_process_alive: MagicMock,
        mock_get_service_status: MagicMock,
        mock_is_daemon_healthy: MagicMock,
        mock_monotonic: MagicMock,
        mock_sleep: MagicMock,
    ) -> None:
        """Service stop waits past 30s until pid, service state, and health are clear."""
        from gobby.cli.daemon import _wait_for_service_stop

        mock_is_process_alive.side_effect = [True, False, False, False]
        mock_get_service_status.side_effect = [
            {"running": True},
            {"running": True},
            {"running": False},
            {"running": False},
        ]
        mock_is_daemon_healthy.side_effect = [True, True, True, False]
        mock_monotonic.side_effect = [0.0, 0.0, 15.0, 31.0, 45.0, 45.0]

        elapsed = _wait_for_service_stop(4321, http_port=60887, timeout=75.0, interval=0.25)

        assert elapsed == pytest.approx(45.0)
        assert mock_is_process_alive.call_count == 4
        assert mock_get_service_status.call_count == 4
        assert mock_is_daemon_healthy.call_count == 4
        assert mock_sleep.call_count == 3


class TestStartupProgressPolling:
    """Tests for daemon startup progress polling."""

    @pytest.mark.parametrize(
        "side_effect",
        [
            httpx.DecodingError("invalid json"),
            httpx.ProtocolError("bad protocol"),
            httpx.TooManyRedirects("redirect loop"),
            httpx.RequestError("request failed"),
            RuntimeError("unexpected"),
        ],
    )
    @patch("gobby.cli.daemon.httpx.get")
    def test_non_retryable_startup_progress_errors_return_false(
        self,
        mock_httpx_get: MagicMock,
        side_effect: Exception,
    ) -> None:
        """Non-retryable startup progress failures are reported without escaping."""
        from gobby.cli.daemon import _poll_startup_progress

        mock_httpx_get.side_effect = side_effect

        assert _poll_startup_progress(60887, max_wait=5.0) is False


def test_reconcile_ui_exposure_reports_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "gobby.cli.daemon.reconcile_ui_exposure",
        lambda _port: UiExposeResult(
            mode="tailscale",
            url="https://host.tailnet.ts.net/",
            changed=False,
        ),
    )

    _reconcile_ui_exposure(60887)

    assert "Web UI exposed at https://host.tailnet.ts.net/" in capsys.readouterr().out


def test_reconcile_ui_exposure_warns_without_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_port: int) -> None:
        raise UiExposeError("tailscale is unavailable")

    monkeypatch.setattr("gobby.cli.daemon.reconcile_ui_exposure", fail)

    _reconcile_ui_exposure(60887)

    assert "warning: UI exposure reconciliation failed: tailscale is unavailable" in (
        capsys.readouterr().out
    )


class TestStartCommand:
    """Tests for the 'start' command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture(autouse=True)
    def mock_ui_exposure_reconciliation(self) -> Generator[MagicMock]:
        with patch("gobby.cli.daemon._reconcile_ui_exposure") as reconcile:
            yield reconcile

    @pytest.fixture(autouse=True)
    def mock_service_admission(self) -> Generator[MagicMock]:
        with patch("gobby.cli.daemon.admit_service_start", return_value=None) as admit:
            yield admit

    def test_start_help(self, runner: CliRunner) -> None:
        """Test start --help displays help text."""
        removed_option = "--no-" + "ui"
        result = runner.invoke(cli, ["start", "--help"])
        assert result.exit_code == 0
        assert "--docker" not in result.output
        assert removed_option not in result.output
        assert "Start the Gobby daemon" in result.output
        assert "--verbose" in result.output

    def test_start_rejects_removed_ui_suppression_option(self, runner: CliRunner) -> None:
        """The removed one-shot UI suppression option fails at the CLI boundary."""
        removed_option = "--no-" + "ui"
        result = runner.invoke(cli, ["start", removed_option])

        assert result.exit_code == 2
        assert f"No such option '{removed_option}'" in result.output

    def test_start_rejects_removed_docker_option(self, runner: CliRunner) -> None:
        """The removed start --docker option fails at the CLI boundary."""
        result = runner.invoke(cli, ["start", "--docker"])

        assert result.exit_code == 2
        assert "No such option '--docker'" in result.output

    @pytest.mark.parametrize(
        ("contents", "mode", "expected_error"),
        [
            (
                "datastore_mode: invalid\n",
                0o600,
                "datastore_mode must be one of: local, remote",
            ),
            pytest.param(
                "datastore_mode: local\n",
                0o644,
                "permissions must be 0600",
                marks=pytest.mark.skipif(os.name == "nt", reason="Unix permissions only"),
            ),
        ],
    )
    def test_start_reports_invalid_bootstrap_without_traceback(
        self,
        tmp_path: Path,
        runner: CliRunner,
        contents: str,
        mode: int,
        expected_error: str,
    ) -> None:
        bootstrap_path = tmp_path / "bootstrap.yaml"
        bootstrap_path.write_text(contents)
        bootstrap_path.chmod(mode)

        with (
            patch("gobby.cli.daemon._start_dependency_errors", _start_dependency_errors),
            patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
        ):
            result = runner.invoke(cli, ["start"])

        output_lines = result.output.strip().splitlines()
        assert result.exit_code == 1
        assert len(output_lines) == 1
        assert "Invalid bootstrap.yaml:" in output_lines[0]
        assert expected_error in output_lines[0]
        assert "Traceback" not in result.output

    def test_start_rejects_unhealthy_dependency_before_services(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runner: CliRunner,
    ) -> None:
        services_start = MagicMock()
        monkeypatch.setattr(
            "gobby.cli.daemon._start_dependency_errors",
            lambda: [
                "Git is outdated; detected 2.37.0, requires >=2.38.0. "
                "Install Git 2.38.0 or newer and retry."
            ],
        )
        monkeypatch.setattr("gobby.cli.daemon._services_start", services_start)

        result = runner.invoke(cli, ["start"])

        assert result.exit_code == 1
        assert "Git is outdated" in result.output
        assert "detected 2.37.0" in result.output
        assert "requires >=2.38.0" in result.output
        services_start.assert_not_called()

    @patch("gobby.cli.daemon._poll_startup_progress", return_value=True)
    @patch("gobby.cli.daemon._wait_for_daemon_health", return_value=2.5)
    @patch("gobby.cli.daemon.service_start", return_value={"success": True})
    @patch(
        "gobby.cli.daemon.get_service_status", return_value={"installed": True, "platform": "macos"}
    )
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_via_service_waits_for_health(
        self,
        mock_load_config: MagicMock,
        mock_get_service_status: MagicMock,
        mock_service_start: MagicMock,
        mock_wait_for_health: MagicMock,
        mock_poll_startup: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        mock_ui_exposure_reconciliation: MagicMock,
    ) -> None:
        """Start waits for health before returning when using the service manager."""
        mock_load_config.return_value = mock_daemon_config
        call_order: list[str] = []

        def record_ready(_port: int) -> bool:
            call_order.append("ready")
            return True

        def record_reconciliation(_port: int) -> None:
            call_order.append("reconcile")

        mock_poll_startup.side_effect = record_ready
        mock_ui_exposure_reconciliation.side_effect = record_reconciliation

        result = runner.invoke(cli, ["start"])

        assert result.exit_code == 0
        assert "Starting via OS service manager" in result.output
        assert "Start request accepted by macos service manager" in result.output
        assert "Waiting for daemon health via service" in result.output
        assert "Health check passed (2.5s)" in result.output
        mock_service_start.assert_called_once()
        mock_wait_for_health.assert_called_once_with(mock_daemon_config.daemon_port)
        mock_poll_startup.assert_called_once_with(mock_daemon_config.daemon_port)
        mock_ui_exposure_reconciliation.assert_called_once_with(mock_daemon_config.daemon_port)
        assert call_order == ["ready", "reconcile"]

    @patch("gobby.cli.daemon._poll_startup_progress", return_value=True)
    @patch("gobby.cli.daemon._wait_for_daemon_health", return_value=2.5)
    @patch("gobby.cli.daemon._services_start")
    @patch("gobby.cli.daemon.service_start", return_value={"success": True})
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": True, "platform": "macos"},
    )
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_via_service_starts_docker_dependencies_first(
        self,
        mock_load_config: MagicMock,
        mock_get_gobby_home: MagicMock,
        _mock_get_service_status: MagicMock,
        mock_service_start: MagicMock,
        mock_services_start: MagicMock,
        _mock_wait_for_health: MagicMock,
        _mock_poll_startup: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Managed service startup first revives configured Docker dependencies."""
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        (services_dir / "docker-compose.yml").touch()
        mock_get_gobby_home.return_value = tmp_path
        mock_load_config.return_value = mock_daemon_config
        call_order: list[str] = []

        def start_services(_home: Path) -> object:
            from gobby.cli._daemon_services import ServiceStartResult

            call_order.append("docker")
            return ServiceStartResult("success", "Docker services started")

        def start_service(**_kwargs: object) -> dict[str, bool]:
            call_order.append("service")
            return {"success": True}

        mock_services_start.side_effect = start_services
        mock_service_start.side_effect = start_service

        result = runner.invoke(cli, ["start"])

        assert result.exit_code == 0
        assert call_order == ["docker", "service"]
        mock_services_start.assert_called_once_with(tmp_path)

    def test_start_stops_before_service_manager_when_docker_start_fails(
        self,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        from gobby.cli._daemon_services import ServiceStartResult

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        (services_dir / "docker-compose.yml").touch()
        with (
            patch("gobby.cli.runtime.CliRuntime.require_config", return_value=mock_daemon_config),
            patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
            patch(
                "gobby.cli.daemon._services_start",
                return_value=ServiceStartResult("failed", "compose failed"),
            ),
            patch("gobby.cli.daemon.service_start") as mock_service_start,
        ):
            result = runner.invoke(cli, ["start"])

        assert result.exit_code == 1
        assert "compose failed" in result.output
        assert "Docker services started" not in result.output
        mock_service_start.assert_not_called()

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_success(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test successful daemon start."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {"process": {}}

        # Mock process
        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None  # Process is running
        mock_popen.return_value = mock_process

        # Mock successful health check
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            # Create necessary directories within temp_dir by setting HOME
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"], env={"HOME": str(temp_dir)})

            assert result.exit_code == 0
            assert "PostgreSQL hub initialized" in result.output
            mock_init_storage.assert_called_once()
            popen_stdout = mock_popen.call_args.kwargs["stdout"]
            popen_stderr = mock_popen.call_args.kwargs["stderr"]
            assert popen_stdout is popen_stderr
            assert popen_stdout.name == str(
                resolved_log_path(mock_daemon_config.logging, RUNTIME_LOG_FILENAME)
            )
            mock_popen.assert_called_once()

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_warns_when_no_agent_auth_env_detected(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Start emits a soft warning when no major CLI auth env is visible."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {"process": {}}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
            patch("gobby.cli.daemon.has_auth_env", return_value=False),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"], env={"HOME": str(temp_dir)})

        assert result.exit_code == 0
        assert "no Anthropic/OpenAI/Qwen API/provider credential env vars detected" in (
            result.output
        )

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_with_verbose_flag(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start with --verbose flag adds verbose argument to command."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {"process": {}}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start", "--verbose"])

            assert result.exit_code == 0
            # Check that --verbose was passed to the subprocess command
            call_args = mock_popen.call_args
            cmd = call_args[0][0]
            assert "--verbose" in cmd

    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_daemon_already_running(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start when a live daemon holds the singleton lock."""
        from gobby.runner_pid_file import claim_pid_file

        mock_load_config.return_value = mock_daemon_config
        gobby_dir = temp_dir / ".gobby"

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
            patch("gobby.cli.daemon.get_gobby_home", return_value=gobby_dir),
        ):
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            # Hold the daemon lock as the "running daemon" (flock conflicts
            # across file descriptors even within one process).
            pid_file = gobby_dir / "gobby.pid"
            claim = claim_pid_file(pid_file)
            assert claim is not None
            try:
                result = runner.invoke(cli, ["start"])
            finally:
                claim.release()

            assert result.exit_code == 1
            assert f"already running (PID: {os.getpid()})" in result.output
            assert pid_file.read_text() == str(os.getpid())
            mock_kill_daemons.assert_not_called()
            mock_init_storage.assert_not_called()

    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_removes_stale_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start removes stale PID file when process not running."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        gobby_dir = temp_dir / ".gobby"

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
            patch("gobby.cli.daemon.get_gobby_home", return_value=gobby_dir),
        ):
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            # Create PID file with a non-existent PID
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text("99999999")

            # The test will proceed to try starting the daemon after removing
            # stale PID - mock the remaining calls to prevent actual daemon start
            with (
                patch("gobby.cli.daemon.is_port_available", return_value=True),
                patch("gobby.cli.daemon.subprocess.Popen") as mock_popen,
                patch("gobby.cli.daemon.httpx.get") as mock_httpx_get,
                patch("gobby.cli.daemon.time.sleep"),
            ):
                mock_process = MagicMock()
                mock_process.pid = 12345
                mock_process.poll.return_value = None
                mock_popen.return_value = mock_process

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_httpx_get.return_value = mock_response

                result = runner.invoke(cli, ["start"])

                # Stale PID file is silently removed, daemon starts successfully
                assert result.exit_code == 0
                assert "Daemon process launched" in result.output

    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_http_port_in_use_timeout(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start fails when HTTP port never becomes available."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = False
        mock_wait_port.return_value = False  # Port never available

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Port" in result.output and "still in use" in result.output

    @patch("gobby.cli.daemon.wait_for_port_available")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_websocket_port_in_use_timeout(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_wait_port: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start fails when WebSocket port never becomes available."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0

        # HTTP port available, WS port not
        daemon_port = int(mock_daemon_config.daemon_port)

        def port_available_side_effect(port: int, **_kwargs: object) -> bool:
            return port == daemon_port

        mock_is_port_available.side_effect = port_available_side_effect
        mock_wait_port.return_value = False

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Port" in result.output and "still in use" in result.output

    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_process_exits_immediately(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start handles process that exits immediately."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = 1  # Process exited
        mock_popen.return_value = mock_process

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Daemon process exited immediately" in result.output

    @patch("gobby.cli.daemon._wait_for_daemon_health", return_value=None)
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_health_check_fails(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_wait_for_health: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test start exits when health check fails."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_httpx_get.side_effect = httpx.ConnectError("Connection refused")

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Health check failed" in result.output
            mock_wait_for_health.assert_called_once_with(mock_daemon_config.daemon_port)

    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_kills_existing_processes(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """A leftover pid file is not treated as a live daemon."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 2
        gobby_dir = temp_dir / ".gobby"

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
            patch("gobby.cli.daemon.get_gobby_home", return_value=gobby_dir),
        ):
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)
            (gobby_dir / "gobby.pid").write_text("99999999")

            with (
                patch("gobby.cli.daemon.is_port_available", return_value=True),
                patch("gobby.cli.daemon.subprocess.Popen") as mock_popen,
                patch("gobby.cli.daemon.httpx.get") as mock_httpx_get,
                patch("gobby.cli.daemon.fetch_rich_status", return_value={}),
            ):
                mock_process = MagicMock()
                mock_process.pid = 12345
                mock_process.poll.return_value = None
                mock_popen.return_value = mock_process

                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_httpx_get.return_value = mock_response

                result = runner.invoke(cli, ["start"])

                assert result.exit_code == 0
                assert "Stopped 2 existing process(es)" not in result.output
                mock_kill_daemons.assert_not_called()


class TestStopCommand:
    """Tests for the 'stop' command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_stop_help(self, runner: CliRunner) -> None:
        """Test stop --help displays help text."""
        result = runner.invoke(cli, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Stop the Gobby daemon" in result.output

    @patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_stop_success(
        self,
        mock_load_config: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test successful daemon stop."""
        mock_load_config.return_value = MagicMock()
        mock_stop_daemon.return_value = True

        result = runner.invoke(cli, ["stop"])

        assert result.exit_code == 0
        mock_stop_daemon.assert_called_once_with(
            quiet=False,
            shutdown_intent="stop",
            shutdown_source="cli_stop",
        )
        assert mock_stop_daemon.call_count == 1
        assert mock_stop_daemon.call_args is not None

    @patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_stop_failure(
        self,
        mock_load_config: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test stop command fails when stop_daemon returns False."""
        mock_load_config.return_value = MagicMock()
        mock_stop_daemon.return_value = False

        result = runner.invoke(cli, ["stop"])

        assert result.exit_code == 1
        mock_stop_daemon.assert_called_once_with(
            quiet=False,
            shutdown_intent="stop",
            shutdown_source="cli_stop",
        )
        assert mock_stop_daemon.call_count == 1
        assert mock_stop_daemon.call_args is not None

    @patch("gobby.cli.daemon._wait_for_service_stop", return_value=1.5)
    @patch("gobby.cli.daemon.service_stop", return_value={"success": True})
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": True, "running": True, "platform": "macos", "pid": 4321},
    )
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_stop_via_service_waits_for_shutdown(
        self,
        mock_load_config: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        mock_service_stop: MagicMock,
        mock_wait_for_service_stop: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Service-managed stop waits for the daemon to exit before returning."""
        from gobby.cli.daemon import SERVICE_MANAGED_STOP_TIMEOUT_SECONDS

        mock_config = MagicMock()
        mock_config.daemon_port = 60887
        mock_load_config.return_value = mock_config

        result = runner.invoke(cli, ["stop"])

        assert result.exit_code == 0
        assert "Stopping via OS service manager" in result.output
        assert "Waiting for service-managed daemon (PID: 4321) to exit" in result.output
        assert "Daemon stopped via macos service (1.5s)" in result.output
        mock_stop_daemon.assert_not_called()
        mock_service_stop.assert_called_once_with(
            shutdown_intent="stop",
            shutdown_source="cli_stop",
        )
        mock_wait_for_service_stop.assert_called_once_with(
            4321,
            http_port=60887,
            timeout=SERVICE_MANAGED_STOP_TIMEOUT_SECONDS,
        )

    @patch("gobby.runner_maintenance.write_shutdown_source")
    @patch("gobby.cli.installers.service.subprocess.run")
    @patch("gobby.cli.installers.service._plist_path")
    @patch("gobby.cli.installers.service.sys")
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": True, "running": True, "platform": "macos", "pid": 4321},
    )
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_stop_via_service_falls_back_when_launchctl_bootout_fails(
        self,
        mock_load_config: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        mock_sys: MagicMock,
        mock_plist_path: MagicMock,
        mock_run: MagicMock,
        mock_write_shutdown_source: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        """Real macOS bootout errors are surfaced so direct stop fallback can run."""
        mock_load_config.return_value = MagicMock()
        mock_sys.platform = "darwin"
        plist = tmp_path / "com.gobby.daemon.plist"
        plist.write_text("<plist>test</plist>")
        mock_plist_path.return_value = plist
        mock_run.return_value = MagicMock(
            returncode=5,
            stderr="Input/output error",
            stdout="",
        )

        result = runner.invoke(cli, ["stop"])

        assert result.exit_code == 0
        assert "Service stop failed: launchctl bootout failed: Input/output error" in result.output
        assert "Falling back to direct stop" in result.output
        mock_stop_daemon.assert_called_once_with(
            quiet=False,
            shutdown_intent="stop",
            shutdown_source="cli_stop",
        )
        mock_write_shutdown_source.assert_called_once_with("cli_stop", intent="stop")


class TestRestartCommand:
    """Tests for the 'restart' command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture(autouse=True)
    def mock_service_admission(self) -> Generator[MagicMock]:
        with patch("gobby.cli.daemon.admit_service_start", return_value=None) as admit:
            yield admit

    def test_restart_help(self, runner: CliRunner) -> None:
        """Test restart --help displays help text."""
        removed_option = "--no-" + "ui"
        result = runner.invoke(cli, ["restart", "--help"])
        assert result.exit_code == 0
        assert removed_option not in result.output
        assert "Restart the Gobby daemon" in result.output
        assert "--verbose" in result.output

    def test_restart_rejects_removed_ui_suppression_option(self, runner: CliRunner) -> None:
        """The removed one-shot UI suppression option fails at the CLI boundary."""
        removed_option = "--no-" + "ui"
        result = runner.invoke(cli, ["restart", removed_option])

        assert result.exit_code == 2
        assert f"No such option '{removed_option}'" in result.output

    @patch("gobby.cli.daemon._poll_startup_progress", return_value=True)
    @patch("gobby.cli.daemon._wait_for_daemon_health", return_value=4.0)
    @patch("gobby.cli.daemon._wait_for_service_stop", return_value=1.0)
    @patch("gobby.cli.daemon.service_start", return_value={"success": True})
    @patch("gobby.cli.daemon.service_stop", return_value={"success": True})
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={
            "installed": True,
            "enabled": True,
            "running": True,
            "platform": "macos",
            "pid": 4321,
        },
    )
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_restart_via_service_waits_for_health(
        self,
        mock_load_config: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        mock_service_stop: MagicMock,
        mock_service_start: MagicMock,
        mock_wait_for_service_stop: MagicMock,
        mock_wait_for_health: MagicMock,
        mock_poll_startup: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
    ) -> None:
        """Restart runs a full service-managed stop and start."""
        from gobby.cli.daemon import SERVICE_MANAGED_STOP_TIMEOUT_SECONDS

        mock_load_config.return_value = mock_daemon_config

        result = runner.invoke(cli, ["restart"])

        assert result.exit_code == 0
        assert "Stopping via OS service manager" in result.output
        assert "Waiting for service-managed daemon (PID: 4321) to exit" in result.output
        assert "Daemon stopped via macos service (1.0s)" in result.output
        assert "Starting via OS service manager" in result.output
        assert "Start request accepted by macos service manager" in result.output
        assert "Waiting for daemon health via service" in result.output
        assert "Daemon started via macos service" in result.output
        assert "Health check passed (4.0s)" in result.output
        mock_stop_daemon.assert_not_called()
        mock_service_stop.assert_called_once_with(
            shutdown_intent="restart",
            shutdown_source="cli_restart",
        )
        mock_service_start.assert_called_once()
        mock_wait_for_service_stop.assert_called_once_with(
            4321,
            http_port=mock_daemon_config.daemon_port,
            timeout=SERVICE_MANAGED_STOP_TIMEOUT_SECONDS,
        )
        mock_wait_for_health.assert_called_once_with(mock_daemon_config.daemon_port)
        mock_poll_startup.assert_called_once_with(mock_daemon_config.daemon_port)

    @patch("gobby.cli.daemon._poll_startup_progress", return_value=True)
    @patch("gobby.cli.daemon._wait_for_daemon_health", return_value=4.0)
    @patch("gobby.cli.daemon._wait_for_service_stop", return_value=45.0)
    @patch("gobby.cli.daemon.service_start", return_value={"success": True})
    @patch("gobby.cli.daemon.service_stop", return_value={"success": True})
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={
            "installed": True,
            "enabled": True,
            "running": True,
            "platform": "macos",
            "pid": 4321,
        },
    )
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_restart_via_service_allows_slow_launchd_stop(
        self,
        mock_load_config: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        mock_service_stop: MagicMock,
        mock_service_start: MagicMock,
        mock_wait_for_service_stop: MagicMock,
        mock_wait_for_health: MagicMock,
        mock_poll_startup: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
    ) -> None:
        """Restart proceeds when launchd stop takes longer than the old 30s wait."""
        from gobby.cli.daemon import SERVICE_MANAGED_STOP_TIMEOUT_SECONDS

        mock_load_config.return_value = mock_daemon_config

        result = runner.invoke(cli, ["restart"])

        assert result.exit_code == 0
        assert "Daemon stopped via macos service (45.0s)" in result.output
        mock_stop_daemon.assert_not_called()
        mock_wait_for_service_stop.assert_called_once_with(
            4321,
            http_port=mock_daemon_config.daemon_port,
            timeout=SERVICE_MANAGED_STOP_TIMEOUT_SECONDS,
        )
        mock_service_start.assert_called_once()
        mock_wait_for_health.assert_called_once_with(mock_daemon_config.daemon_port)
        mock_poll_startup.assert_called_once_with(mock_daemon_config.daemon_port)

    @patch("gobby.cli.daemon._poll_startup_progress", return_value=False)
    @patch("gobby.cli.daemon._wait_for_daemon_health", return_value=4.0)
    @patch("gobby.cli.daemon._wait_for_service_stop", return_value=1.0)
    @patch("gobby.cli.daemon.service_start", return_value={"success": True})
    @patch("gobby.cli.daemon.service_stop", return_value={"success": True})
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={
            "installed": True,
            "enabled": True,
            "running": True,
            "platform": "macos",
            "pid": 4321,
        },
    )
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_restart_via_service_fails_when_startup_readiness_does_not_complete(
        self,
        mock_load_config: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        mock_service_stop: MagicMock,
        mock_service_start: MagicMock,
        mock_wait_for_service_stop: MagicMock,
        mock_wait_for_health: MagicMock,
        mock_poll_startup: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
    ) -> None:
        """Restart exits non-zero when startup readiness never completes."""
        mock_load_config.return_value = mock_daemon_config

        result = runner.invoke(cli, ["restart"])

        assert result.exit_code == 1
        assert "Daemon did not finish startup readiness after service start" in result.output
        mock_poll_startup.assert_called_once_with(mock_daemon_config.daemon_port)

    @patch("gobby.cli.daemon._poll_startup_progress")
    @patch("gobby.cli.daemon._wait_for_daemon_health", return_value=None)
    @patch("gobby.cli.daemon._wait_for_service_stop", return_value=0.8)
    @patch("gobby.cli.daemon.service_start", return_value={"success": True})
    @patch("gobby.cli.daemon.service_stop", return_value={"success": True})
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={
            "installed": True,
            "enabled": True,
            "running": True,
            "platform": "macos",
            "pid": 4321,
        },
    )
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_restart_via_service_fails_when_health_does_not_return(
        self,
        mock_load_config: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        mock_service_stop: MagicMock,
        mock_service_start: MagicMock,
        mock_wait_for_service_stop: MagicMock,
        mock_wait_for_health: MagicMock,
        mock_poll_startup: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
    ) -> None:
        """Restart exits non-zero when the service start never becomes healthy."""
        from gobby.cli.daemon import SERVICE_MANAGED_STOP_TIMEOUT_SECONDS

        mock_load_config.return_value = mock_daemon_config

        result = runner.invoke(cli, ["restart"])

        assert result.exit_code == 1
        assert "Daemon stopped via macos service (0.8s)" in result.output
        assert "Daemon did not become healthy after service start" in result.output
        mock_stop_daemon.assert_not_called()
        mock_service_stop.assert_called_once_with(
            shutdown_intent="restart",
            shutdown_source="cli_restart",
        )
        mock_service_start.assert_called_once()
        mock_wait_for_service_stop.assert_called_once_with(
            4321,
            http_port=mock_daemon_config.daemon_port,
            timeout=SERVICE_MANAGED_STOP_TIMEOUT_SECONDS,
        )
        mock_wait_for_health.assert_called_once_with(mock_daemon_config.daemon_port)
        mock_poll_startup.assert_not_called()

    @patch("gobby.cli.daemon._wait_for_daemon_health")
    @patch("gobby.cli.daemon.service_start")
    @patch("gobby.cli.daemon._wait_for_service_stop", return_value=None)
    @patch("gobby.cli.daemon.service_stop", return_value={"success": True})
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={
            "installed": True,
            "enabled": True,
            "running": True,
            "platform": "macos",
            "pid": 4321,
        },
    )
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_restart_via_service_fails_when_stop_does_not_complete(
        self,
        mock_load_config: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_get_service_status: MagicMock,
        mock_service_stop: MagicMock,
        mock_wait_for_service_stop: MagicMock,
        mock_service_start: MagicMock,
        mock_wait_for_health: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
    ) -> None:
        """Restart exits non-zero when the stop phase never shuts down."""
        from gobby.cli.daemon import SERVICE_MANAGED_STOP_TIMEOUT_SECONDS

        mock_load_config.return_value = mock_daemon_config

        result = runner.invoke(cli, ["restart"])

        assert result.exit_code == 1
        expected_timeout = f"{SERVICE_MANAGED_STOP_TIMEOUT_SECONDS:.0f}s"
        assert (
            f"Service stop returned, but daemon is still running after {expected_timeout}"
            in result.output
        )
        mock_stop_daemon.assert_not_called()
        mock_service_stop.assert_called_once_with(
            shutdown_intent="restart",
            shutdown_source="cli_restart",
        )
        mock_wait_for_service_stop.assert_called_once_with(
            4321,
            http_port=mock_daemon_config.daemon_port,
            timeout=SERVICE_MANAGED_STOP_TIMEOUT_SECONDS,
        )
        mock_service_start.assert_not_called()
        mock_wait_for_health.assert_not_called()

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_restart_success(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test successful daemon restart."""
        mock_load_config.return_value = mock_daemon_config
        mock_stop_daemon.return_value = True
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {"process": {}}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
            patch("gobby.cli.daemon.get_service_status", return_value={"installed": False}),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["restart"])

            assert result.exit_code == 0
            assert "Starting Gobby daemon" in result.output
            mock_stop_daemon.assert_called_once()
            mock_setup_logging.assert_not_called()
            mock_fetch_status.assert_not_called()

    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
    def test_restart_stop_fails(
        self,
        mock_service_status: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test restart aborts when stop fails."""
        mock_stop_daemon.return_value = False

        result = runner.invoke(cli, ["restart"])

        assert result.exit_code == 1
        mock_stop_daemon.assert_called_once_with(
            quiet=False,
            shutdown_intent="restart",
            shutdown_source="cli_restart",
        )

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.stop_daemon_util")
    @patch("gobby.cli.daemon.setup_logging")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_restart_with_verbose(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_setup_logging: MagicMock,
        mock_stop_daemon: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test restart with --verbose flag."""
        mock_load_config.return_value = mock_daemon_config
        mock_stop_daemon.return_value = True
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
            patch("gobby.cli.daemon.get_service_status", return_value={"installed": False}),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["restart", "--verbose"])

            assert result.exit_code == 0
            mock_setup_logging.assert_called_once_with(True)
            assert mock_setup_logging.call_count == 1
            assert mock_setup_logging.call_args is not None
            mock_fetch_status.assert_not_called()


class TestStatusCommand:
    """Tests for the 'status' command."""

    @pytest.fixture(autouse=True)
    def mock_port_listener_pid(self) -> Generator[MagicMock]:
        with patch("gobby.cli.daemon.get_port_listener_pid", return_value=None) as mock_probe:
            yield mock_probe

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_status_help(self, runner: CliRunner) -> None:
        """Test status --help displays help text."""
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "Show Gobby daemon operational health dashboard" in result.output

    def test_status_reports_native_windows_as_unsupported(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runner: CliRunner,
    ) -> None:
        monkeypatch.setattr(
            "gobby.cli.daemon.unsupported_platform_error",
            lambda: "Native Windows is unsupported; use WSL 2.",
        )

        result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        assert "Unsupported (native Windows; use WSL 2)" in result.output

    @patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_status_no_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_get_gobby_home: MagicMock,
        mock_get_service_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status when no PID file exists."""
        mock_load_config.return_value = mock_daemon_config

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            # Create gobby dir without PID file
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            with patch("gobby.cli.runtime.CliRuntime.require_database", return_value=MagicMock()):
                result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Stopped" in result.output

    @patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_status_invalid_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_get_gobby_home: MagicMock,
        mock_get_service_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status with invalid PID file content."""
        mock_load_config.return_value = mock_daemon_config

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            # Create invalid PID file
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text("not-a-number")

            with patch("gobby.cli.runtime.CliRuntime.require_database", return_value=MagicMock()):
                result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Stopped" in result.output

    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_status_stale_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_get_gobby_home: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status with stale PID file (process not running)."""
        mock_load_config.return_value = mock_daemon_config

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            # Create PID file with non-existent process
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text("99999999")

            with patch("gobby.cli.runtime.CliRuntime.require_database", return_value=MagicMock()):
                result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Stopped" in result.output
            assert "Uptime:" not in result.output

    @patch("gobby.utils.deps.check_config_mismatches", return_value=[])
    @patch(
        "gobby.utils.deps.collect_all_deps",
        return_value={"gobby": {}, "coding_clis": {}, "dependencies": {}},
    )
    @patch("gobby.cli.daemon.probe_daemon_lock")
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch(
        "gobby.cli.daemon.fetch_rich_status",
        return_value=RichStatusProbe(api_data={"process": {}}, health_confirmed=True),
    )
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.daemon._is_process_alive", side_effect=[False, True])
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_status_uses_live_listener_for_stale_pid_file(
        self,
        mock_load_config: MagicMock,
        mock_is_process_alive: MagicMock,
        mock_psutil_process: MagicMock,
        mock_fetch_status: MagicMock,
        mock_get_gobby_home: MagicMock,
        mock_probe: MagicMock,
        mock_collect_deps: MagicMock,
        mock_check_mismatches: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        mock_port_listener_pid: MagicMock,
    ) -> None:
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        reported_pid = 40286
        listener_pid = 67485
        mock_probe.return_value = SingletonProbe(
            state=ProbeState.DAEMON, pid=reported_pid, role="daemon"
        )
        mock_load_config.return_value = mock_daemon_config
        mock_port_listener_pid.return_value = listener_pid
        mock_psutil_process.return_value.create_time.return_value = 2800.0

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)
            (gobby_dir / "gobby.pid").write_text(str(reported_pid))
            mock_get_gobby_home.return_value = gobby_dir

            with (
                patch("gobby.cli.daemon.time.time", return_value=10000.0),
                patch("gobby.cli.runtime.CliRuntime.require_database", return_value=MagicMock()),
            ):
                result = runner.invoke(cli, ["status"])

        assert result.exit_code == 0
        assert f"Running (PID: {listener_pid})" in result.output
        assert "Uptime:" in result.output
        assert "2h" in result.output
        assert f"Note: Stale PID file found (PID {reported_pid})" in result.output
        assert (
            f"Note: PID mismatch: PID file reports {reported_pid}; "
            f"HTTP port {mock_daemon_config.daemon_port} is owned by PID {listener_pid}"
            in result.output
        )
        mock_psutil_process.assert_called_once_with(listener_pid)

    @patch("gobby.cli.daemon._is_process_alive", return_value=False)
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_status_uses_process_alive_check(
        self,
        mock_load_config: MagicMock,
        mock_get_gobby_home: MagicMock,
        mock_is_process_alive: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status treats a PID rejected by the liveness helper as stopped."""
        mock_load_config.return_value = mock_daemon_config

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text(str(os.getpid()))

            result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Stopped" in result.output
            mock_is_process_alive.assert_not_called()

    @patch("gobby.utils.deps.check_config_mismatches", return_value=[])
    @patch(
        "gobby.utils.deps.collect_all_deps",
        return_value={"gobby": {}, "coding_clis": {}, "dependencies": {}},
    )
    @patch("gobby.cli.daemon.probe_daemon_lock")
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_status_daemon_running(
        self,
        mock_load_config: MagicMock,
        mock_psutil_process: MagicMock,
        mock_fetch_status: MagicMock,
        mock_get_gobby_home: MagicMock,
        mock_probe: MagicMock,
        mock_collect_deps: MagicMock,
        mock_check_mismatches: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        mock_port_listener_pid: MagicMock,
    ) -> None:
        """Test status when daemon is running."""
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        mock_probe.return_value = SingletonProbe(
            state=ProbeState.DAEMON, pid=os.getpid(), role="daemon"
        )
        mock_load_config.return_value = mock_daemon_config
        mock_fetch_status.return_value = RichStatusProbe(
            api_data={"process": {}},
            health_confirmed=True,
        )
        mock_port_listener_pid.return_value = os.getpid()

        # Mock psutil.Process
        mock_proc = MagicMock()
        mock_proc.create_time.return_value = time.time() - 3600  # 1 hour ago
        mock_psutil_process.return_value = mock_proc

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)
            mock_get_gobby_home.return_value = gobby_dir

            # Create PID file with current process PID
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text(str(os.getpid()))

            with patch("gobby.cli.runtime.CliRuntime.require_database", return_value=MagicMock()):
                result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "Running" in result.output
            assert "PID mismatch" not in result.output
            mock_fetch_status.assert_called_once()

    @patch("gobby.utils.deps.check_config_mismatches", return_value=[])
    @patch(
        "gobby.utils.deps.collect_all_deps",
        return_value={"gobby": {}, "coding_clis": {}, "dependencies": {}},
    )
    @patch("gobby.cli.daemon.probe_daemon_lock")
    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.daemon._is_process_alive", return_value=True)
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_status_psutil_error(
        self,
        mock_load_config: MagicMock,
        mock_is_process_alive: MagicMock,
        mock_psutil_process: MagicMock,
        mock_fetch_status: MagicMock,
        mock_probe: MagicMock,
        mock_collect_deps: MagicMock,
        mock_check_mismatches: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status handles psutil errors gracefully."""
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        mock_probe.return_value = SingletonProbe(
            state=ProbeState.DAEMON, pid=os.getpid(), role="daemon"
        )
        mock_load_config.return_value = mock_daemon_config
        mock_fetch_status.return_value = RichStatusProbe(
            api_data={"process": {}},
            health_confirmed=True,
        )
        mock_psutil_process.side_effect = psutil.NoSuchProcess(pid=12345)

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            # Create PID file with current process PID
            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text(str(os.getpid()))

            with patch("gobby.cli.daemon.get_gobby_home", return_value=gobby_dir):
                with patch(
                    "gobby.cli.runtime.CliRuntime.require_database", return_value=MagicMock()
                ):
                    result = runner.invoke(cli, ["status"])

            # Should still work, just without uptime info
            assert result.exit_code == 0
            assert "Running" in result.output
            mock_is_process_alive.assert_called_once_with(os.getpid())


class TestDaemonCommandsIntegration:
    """Integration tests for daemon commands."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def clean_pid_file(self, temp_dir: Path) -> Generator[Path]:
        """Ensure temp PID file location is clean (does NOT touch real PID file)."""
        pid_file = temp_dir / ".gobby" / "gobby.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if pid_file.exists():
            pid_file.unlink()
        yield pid_file
        if pid_file.exists():
            pid_file.unlink()

    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    @pytest.mark.parametrize(
        ("agent_sandbox_enabled", "web_chat_sandbox_enabled"),
        [(True, False), (False, True)],
    )
    def test_start_stops_when_srt_preflight_fails(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file: Path,
        agent_sandbox_enabled: bool,
        web_chat_sandbox_enabled: bool,
    ) -> None:
        """Test that start fails closed when managed SRT is unavailable."""
        mock_load_config.return_value = mock_daemon_config
        mock_daemon_config.ui.enabled = True
        mock_daemon_config.agent_sandbox.enabled = agent_sandbox_enabled
        mock_daemon_config.agent_sandbox.backend = "srt"
        mock_daemon_config.web_chat_sandbox.enabled = web_chat_sandbox_enabled
        mock_daemon_config.web_chat_sandbox.backend = "srt"
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
            patch(
                "gobby.cli.daemon.admit_direct_start",
                return_value=(MagicMock(), None),
            ),
            patch(
                "gobby.cli.runtime.get_cli_runtime",
                return_value=MagicMock(operational_config=mock_daemon_config),
            ),
            patch(
                "gobby.cli.runtime.CliRuntime.require_database",
                return_value=MagicMock(),
            ),
            patch(
                "gobby.agents.srt_runtime.verify_srt_installation",
                side_effect=SrtRuntimeError("managed runtime unavailable"),
            ),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert (
                "Managed SRT sandbox preflight failed: managed runtime unavailable" in result.output
            )
            assert "Gobby daemon ready" not in result.output
            mock_popen.assert_not_called()

    def test_cli_has_all_daemon_commands(self, runner: CliRunner) -> None:
        """Test that CLI has all daemon management commands."""
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "start" in result.output
        assert "stop" in result.output
        assert "restart" in result.output
        assert "status" in result.output


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def clean_pid_file(self, temp_dir: Path) -> Generator[Path]:
        """Ensure temp PID file location is clean (does NOT touch real PID file)."""
        pid_file = temp_dir / ".gobby" / "gobby.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if pid_file.exists():
            pid_file.unlink()
        yield pid_file
        if pid_file.exists():
            pid_file.unlink()

    @patch("gobby.cli.daemon._wait_for_daemon_health", return_value=None)
    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_health_check_timeout(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        mock_wait_for_health: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file: Path,
    ) -> None:
        """Test start handles health check timeout gracefully."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_httpx_get.side_effect = httpx.TimeoutException("Timeout")

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Health check failed" in result.output
            mock_wait_for_health.assert_called_once_with(mock_daemon_config.daemon_port)

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_health_check_non_200_response(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file: Path,
    ) -> None:
        """Test start retries when health check returns non-200."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        # First few calls fail, then succeed
        responses = []
        for _ in range(5):
            bad_response = MagicMock()
            bad_response.status_code = 500
            responses.append(bad_response)
        good_response = MagicMock()
        good_response.status_code = 200
        responses.append(good_response)
        progress_response = MagicMock()
        progress_response.status_code = 200
        progress_response.json.return_value = {"done": True, "steps_scheduled": []}
        responses.append(progress_response)

        mock_httpx_get.side_effect = responses

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 0
            assert mock_httpx_get.call_count >= len(responses)

    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_popen_exception(
        self,
        mock_load_config: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file: Path,
    ) -> None:
        """Test start handles Popen exception."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_popen.side_effect = OSError("Cannot execute")

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            result = runner.invoke(cli, ["start"])

            assert result.exit_code == 1
            assert "Error starting daemon" in result.output

    @patch("gobby.utils.deps.check_config_mismatches", return_value=[])
    @patch(
        "gobby.utils.deps.collect_all_deps",
        return_value={"gobby": {}, "coding_clis": {}, "dependencies": {}},
    )
    @patch("gobby.cli.daemon.probe_daemon_lock")
    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_status_with_rich_data(
        self,
        mock_load_config: MagicMock,
        mock_psutil_process: MagicMock,
        mock_fetch_status: MagicMock,
        mock_probe: MagicMock,
        mock_collect_deps: MagicMock,
        mock_check_mismatches: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
    ) -> None:
        """Test status command with rich daemon data."""
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        mock_probe.return_value = SingletonProbe(
            state=ProbeState.DAEMON, pid=os.getpid(), role="daemon"
        )
        mock_load_config.return_value = mock_daemon_config
        mock_collect_deps.return_value = {
            "gobby": {},
            "coding_clis": {},
            "integrations": {
                "embeddings_provider": "lmstudio",
                "ollama": {"version": "0.1.30", "running": True},
                "lmstudio": {"running": True},
            },
        }
        mock_fetch_status.return_value = RichStatusProbe(
            api_data={
                "process": {"memory_rss_mb": 128.5, "cpu_percent": 2.5},
                "sessions": {"active": 3, "paused": 0},
                "tasks": {"open": 5, "in_progress": 2},
                "postgres": {
                    "mode": "docker",
                    "dsn_host": "localhost",
                    "dsn_db": "gobby",
                    "database_url": "postgresql://gobby:secret@localhost:60891/gobby",
                    "healthy": True,
                    "extensions": {"pg_search": True, "pgaudit": True, "pgcrypto": True},
                },
            },
            health_confirmed=True,
        )

        mock_proc = MagicMock()
        mock_proc.create_time.return_value = time.time() - 7200  # 2 hours ago
        mock_psutil_process.return_value = mock_proc

        with runner.isolated_filesystem(temp_dir=str(temp_dir)):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            pid_file = gobby_dir / "gobby.pid"
            pid_file.write_text(str(os.getpid()))

            with (
                patch("gobby.cli.daemon.get_gobby_home", return_value=gobby_dir),
                patch("gobby.cli.daemon.get_port_listener_pid", return_value=None),
            ):
                with patch(
                    "gobby.cli.runtime.CliRuntime.require_database", return_value=MagicMock()
                ):
                    result = runner.invoke(cli, ["status"])

            assert result.exit_code == 0
            assert "LM Studio (running)" in result.output
            assert "PostgreSQL:" in result.output
            assert "postgresql://" not in result.output
            assert "secret" not in result.output
            mock_fetch_status.assert_called_once_with(mock_daemon_config.daemon_port, timeout=3.0)


class TestCommandBuilding:
    """Test the command building for subprocess."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture
    def clean_pid_file(self, temp_dir: Path) -> Generator[Path]:
        """Ensure temp PID file location is clean (does NOT touch real PID file)."""
        pid_file = temp_dir / ".gobby" / "gobby.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if pid_file.exists():
            pid_file.unlink()
        yield pid_file
        if pid_file.exists():
            pid_file.unlink()

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_command_uses_correct_module(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file: Path,
    ) -> None:
        """Test that start command builds correct subprocess command."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            runner.invoke(cli, ["start"])

            call_args = mock_popen.call_args
            cmd = call_args[0][0]

            # Check command structure
            assert cmd[0] == sys.executable
            assert "-m" in cmd
            assert "gobby.runner" in cmd

    @patch("gobby.cli.daemon.fetch_rich_status")
    @patch("gobby.cli.daemon.httpx.get")
    @patch("gobby.cli.daemon.subprocess.Popen")
    @patch("gobby.cli.daemon.is_port_available")
    @patch("gobby.cli.daemon.kill_all_gobby_daemons")
    @patch("gobby.cli.daemon.init_local_storage")
    @patch("gobby.cli.daemon.time.sleep")
    @patch("gobby.cli.runtime.CliRuntime.require_config")
    def test_start_subprocess_options(
        self,
        mock_load_config: MagicMock,
        mock_sleep: MagicMock,
        mock_init_storage: MagicMock,
        mock_kill_daemons: MagicMock,
        mock_is_port_available: MagicMock,
        mock_popen: MagicMock,
        mock_httpx_get: MagicMock,
        mock_fetch_status: MagicMock,
        runner: CliRunner,
        mock_daemon_config: MagicMock,
        temp_dir: Path,
        clean_pid_file: Path,
    ) -> None:
        """Test that start command uses correct subprocess options."""
        mock_load_config.return_value = mock_daemon_config
        mock_kill_daemons.return_value = 0
        mock_is_port_available.return_value = True
        mock_fetch_status.return_value = {}

        mock_process = MagicMock()
        mock_process.pid = 12345
        mock_process.poll.return_value = None
        mock_popen.return_value = mock_process

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_httpx_get.return_value = mock_response

        with (
            runner.isolated_filesystem(temp_dir=str(temp_dir)),
            patch("gobby.cli.daemon.Path.home", return_value=temp_dir),
        ):
            gobby_dir = temp_dir / ".gobby"
            gobby_dir.mkdir(parents=True, exist_ok=True)
            (gobby_dir / "logs").mkdir(parents=True, exist_ok=True)

            runner.invoke(cli, ["start"])

            call_kwargs = mock_popen.call_args[1]

            # Check subprocess options
            assert call_kwargs["stdin"] == subprocess.DEVNULL
            assert call_kwargs["start_new_session"] is True
            assert "env" in call_kwargs


def test_start_refuses_linked_worktree_before_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worktree guard fails `gobby start` before dependency checks or services (#21031)."""
    refusal = "Refusing to start the Gobby daemon from linked worktree /wt"
    dependency_errors = MagicMock(return_value=[])
    services_start = MagicMock()
    monkeypatch.setattr("gobby.cli.daemon.worktree_daemon_refusal", lambda: refusal)
    monkeypatch.setattr("gobby.cli.daemon._start_dependency_errors", dependency_errors)
    monkeypatch.setattr("gobby.cli.daemon._services_start", services_start)

    result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code == 1
    assert refusal in result.output
    dependency_errors.assert_not_called()
    services_start.assert_not_called()


def test_restart_refuses_linked_worktree_before_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gobby restart` refuses before stopping the running daemon (#21031)."""
    refusal = "Refusing to start the Gobby daemon from linked worktree /wt"
    do_stop = MagicMock(return_value=True)
    monkeypatch.setattr("gobby.cli.daemon.worktree_daemon_refusal", lambda: refusal)
    monkeypatch.setattr("gobby.cli.daemon._do_stop", do_stop)

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 1
    assert refusal in result.output
    do_stop.assert_not_called()


_PROTECTED_RUN = {
    "run_id": "run-1",
    "job_id": "job-1",
    "job_name": "gobby:memory-dream",
    "started_at": "2026-08-26T07:00:00+00:00",
    "elapsed_seconds": 3725.0,
    "remaining_seconds": 12475.0,
}


@patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
@patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
@patch("gobby.cli.runtime.CliRuntime.require_config")
def test_stop_refuses_while_a_protected_run_is_active(
    mock_load_config: MagicMock,
    mock_stop_daemon: MagicMock,
    _service_status: MagicMock,
    _no_protected_runs: MagicMock,
) -> None:
    """`gobby stop` names the protected run and its elapsed time, then refuses (#21021)."""
    mock_load_config.return_value = MagicMock()
    _no_protected_runs.return_value = [_PROTECTED_RUN]

    result = CliRunner().invoke(cli, ["stop"])

    assert result.exit_code == 1
    assert "gobby:memory-dream (running 1h 2m 5s" in result.output
    assert "--wait" in result.output
    assert "--force" in result.output
    mock_stop_daemon.assert_not_called()


@patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
@patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
@patch("gobby.cli.runtime.CliRuntime.require_config")
def test_stop_force_interrupts_a_protected_run(
    mock_load_config: MagicMock,
    mock_stop_daemon: MagicMock,
    _service_status: MagicMock,
    _no_protected_runs: MagicMock,
) -> None:
    mock_load_config.return_value = MagicMock()
    _no_protected_runs.return_value = [_PROTECTED_RUN]

    result = CliRunner().invoke(cli, ["stop", "--force"])

    assert result.exit_code == 0
    assert "Interrupting protected cron run gobby:memory-dream" in result.output
    mock_stop_daemon.assert_called_once_with(
        quiet=False,
        shutdown_intent="stop",
        shutdown_source="cli_stop",
    )


@patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
@patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
@patch("gobby.cli.runtime.CliRuntime.require_config")
def test_stop_wait_defers_until_the_protected_run_finishes(
    mock_load_config: MagicMock,
    mock_stop_daemon: MagicMock,
    _service_status: MagicMock,
    _no_protected_runs: MagicMock,
) -> None:
    mock_load_config.return_value = MagicMock()
    _no_protected_runs.side_effect = [[_PROTECTED_RUN], []]

    with patch("gobby.cli._daemon_protected_runs.time.sleep") as sleep:
        result = CliRunner().invoke(cli, ["stop", "--wait"])

    assert result.exit_code == 0
    assert "Waiting for protected cron run(s) to finish: gobby:memory-dream" in result.output
    assert "Protected cron run(s) finished" in result.output
    assert sleep.call_count == 1
    mock_stop_daemon.assert_called_once()


@patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
def test_stop_rejects_force_combined_with_wait(mock_stop_daemon: MagicMock) -> None:
    result = CliRunner().invoke(cli, ["stop", "--force", "--wait"])

    assert result.exit_code == 2
    assert "--force and --wait are mutually exclusive" in result.output
    mock_stop_daemon.assert_not_called()


@patch("gobby.cli.daemon.get_service_status", return_value={"installed": False})
@patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
@patch("gobby.cli.daemon.setup_logging")
def test_restart_refuses_while_a_protected_run_is_active(
    _setup_logging: MagicMock,
    mock_stop_daemon: MagicMock,
    _service_status: MagicMock,
    _no_protected_runs: MagicMock,
) -> None:
    """`gobby restart` honors the lease before stopping; `--force` passes through (#21021)."""
    _no_protected_runs.return_value = [_PROTECTED_RUN]

    result = CliRunner().invoke(cli, ["restart"])

    assert result.exit_code == 1
    assert "Refusing to stop the daemon" in result.output
    mock_stop_daemon.assert_not_called()
