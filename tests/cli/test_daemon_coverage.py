"""Tests for cli/daemon.py — targeting uncovered lines."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli._daemon_services import ServiceStartResult
from gobby.cli.daemon import (
    _services_start,
    _services_stop,
    status,
    stop,
)
from gobby.cli.daemon_health import health
from gobby.cli.installers.compose_env import ComposeEnvironmentError, ComposeRuntime
from gobby.cli.runtime import CliRuntime
from gobby.utils.status import EndpointProbeFailure, RichStatusProbe

pytestmark = pytest.mark.unit


def _runtime(*profiles: str) -> ComposeRuntime:
    return ComposeRuntime(
        environment={"GOBBY_FALKORDB_PASSWORD": "password123"},
        profiles=profiles,
    )


def _cli_runtime(config: MagicMock) -> CliRuntime:
    runtime = CliRuntime(config_file=None, config=config)
    runtime._database = MagicMock()
    return runtime


def _write_managed_compose(home: Path) -> None:
    compose = home / "services" / "docker-compose.yml"
    compose.parent.mkdir(parents=True)
    compose.write_text(
        "services:\n"
        "  postgres:\n"
        "    profiles: [postgres]\n"
        "  qdrant:\n"
        "    profiles: [qdrant]\n"
        "  falkordb:\n"
        "    profiles: [falkordb]\n",
        encoding="utf-8",
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# _services_start / _services_stop
# ---------------------------------------------------------------------------
class TestServicesStart:
    @pytest.fixture(autouse=True)
    def _docker_available(self) -> Iterator[None]:
        with patch("shutil.which", return_value="/usr/bin/docker"):
            yield

    def test_no_compose_file(self, tmp_path: Path) -> None:
        """A missing managed Compose asset is a startup failure."""
        result = _services_start(tmp_path)
        assert result.outcome == "failed"
        assert "Compose file is missing" in result.detail
        assert not (tmp_path / "services" / "docker-compose.yml").exists()

    @patch("gobby.cli.daemon.subprocess.run")
    def test_start_exports_persisted_qdrant_port(self, mock_run: MagicMock, tmp_path: Path) -> None:
        _write_managed_compose(tmp_path)

        mock_run.return_value = MagicMock(returncode=0)

        def _resolve(
            _home: Path,
            *,
            profiles: tuple[str, ...] = ("postgres", "qdrant", "falkordb"),
        ) -> ComposeRuntime:
            return ComposeRuntime(
                environment={"GOBBY_QDRANT_HTTP_PORT": "7333"},
                profiles=profiles,
            )

        with patch("gobby.cli.daemon.resolve_compose_runtime", side_effect=_resolve):
            result = _services_start(tmp_path)
        assert result == ServiceStartResult("success", "Docker services started")
        assert mock_run.call_count == 2
        assert mock_run.call_args is not None
        cmd = mock_run.call_args.args[0]
        assert "qdrant" in cmd
        assert mock_run.call_args.kwargs["env"]["GOBBY_QDRANT_HTTP_PORT"] == "7333"

    @patch("gobby.cli.daemon.subprocess.run")
    def test_compose_exists_failure(self, mock_run: MagicMock, tmp_path: Path) -> None:
        _write_managed_compose(tmp_path)

        mock_run.return_value = MagicMock(returncode=1, stderr="err", stdout="")

        with patch(
            "gobby.cli.daemon.resolve_compose_runtime",
            return_value=_runtime("postgres", "qdrant", "falkordb"),
        ):
            result = _services_start(tmp_path)
        assert result.outcome == "failed"
        assert "Docker compose up failed" in result.detail
        mock_run.assert_called_once()
        assert mock_run.call_count == 1
        assert mock_run.call_args is not None
        assert "qdrant" in mock_run.call_args.args[0]
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path / "services")

    @patch("gobby.cli.daemon.subprocess.run")
    def test_compose_timeout(self, mock_run: MagicMock, tmp_path: Path) -> None:
        _write_managed_compose(tmp_path)

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=120)
        with patch(
            "gobby.cli.daemon.resolve_compose_runtime",
            return_value=_runtime("postgres", "qdrant", "falkordb"),
        ):
            result = _services_start(tmp_path)
        assert result.outcome == "failed"
        assert "timed out" in result.detail
        mock_run.assert_called_once()
        assert mock_run.call_count == 1
        assert mock_run.call_args is not None

    def test_config_error(self, tmp_path: Path) -> None:
        _write_managed_compose(tmp_path)

        # Without resolved service config there are no profiles to start.
        with patch("gobby.cli.daemon.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch(
                "gobby.cli.daemon.resolve_compose_runtime",
                side_effect=ComposeEnvironmentError("config error"),
            ):
                result = _services_start(tmp_path)
            assert result.outcome == "failed"
            assert result.detail.endswith("config error")
            assert (tmp_path / "services" / "docker-compose.yml").exists()
            mock_run.assert_not_called()


class TestServicesStop:
    def test_no_compose_file(self, tmp_path: Path) -> None:
        result = _services_stop(tmp_path)
        assert result is False
        assert not (tmp_path / "services" / "docker-compose.yml").exists()

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.return_value = MagicMock(returncode=0)
        with patch("gobby.cli.daemon.resolve_compose_runtime", return_value=_runtime()):
            result = _services_stop(tmp_path)
        assert result is True
        mock_run.assert_called_once()
        assert mock_run.call_count == 1
        assert mock_run.call_args is not None

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_timeout(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=60)
        with patch("gobby.cli.daemon.resolve_compose_runtime", return_value=_runtime()):
            result = _services_stop(tmp_path)
        assert result is False
        assert "Timed out stopping Docker services" in caplog.text
        mock_run.assert_called_once()

    @patch("gobby.cli.daemon.subprocess.run")
    def test_stop_exception(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        compose = tmp_path / "services" / "docker-compose.yml"
        compose.parent.mkdir(parents=True)
        compose.write_text("version: '3'")
        mock_run.side_effect = FileNotFoundError("docker not found")
        with patch("gobby.cli.daemon.resolve_compose_runtime", return_value=_runtime()):
            result = _services_stop(tmp_path)
        assert result is False
        assert "Failed to stop Docker services: docker not found" in caplog.text
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# stop command
# ---------------------------------------------------------------------------
class TestStopCommand:
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": False, "running": False},
    )
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
    def test_stop_success(self, _stop: MagicMock, _svc: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        config.daemon_port = 60887
        result = runner.invoke(stop, [], obj=_cli_runtime(config), catch_exceptions=False)
        assert result.exit_code == 0

    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": False, "running": False},
    )
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=False)
    def test_stop_failure(self, _stop: MagicMock, _svc: MagicMock, runner: CliRunner) -> None:
        config = MagicMock()
        result = runner.invoke(stop, [], obj=_cli_runtime(config), catch_exceptions=False)
        assert result.exit_code == 1

    @patch("gobby.cli.daemon._services_stop", return_value=True)
    @patch("gobby.cli.daemon.get_gobby_home", return_value=Path("/fake"))
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": False, "running": False},
    )
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
    def test_stop_with_docker(
        self,
        _stop: MagicMock,
        _svc: MagicMock,
        _home: MagicMock,
        mock_services: MagicMock,
        runner: CliRunner,
    ) -> None:
        config = MagicMock()
        config.daemon_port = 60887
        result = runner.invoke(stop, ["--docker"], obj=_cli_runtime(config), catch_exceptions=False)
        assert result.exit_code == 0
        mock_services.assert_called_once()
        assert mock_services.call_count == 1
        assert mock_services.call_args is not None

    @patch("gobby.cli.daemon._services_stop", return_value=False)
    @patch("gobby.cli.daemon.get_gobby_home", return_value=Path("/fake"))
    @patch(
        "gobby.cli.daemon.get_service_status",
        return_value={"installed": False, "running": False},
    )
    @patch("gobby.cli.daemon.stop_daemon_util", return_value=True)
    def test_stop_with_docker_reports_container_failure(
        self,
        _stop: MagicMock,
        _svc: MagicMock,
        _home: MagicMock,
        _services: MagicMock,
        runner: CliRunner,
    ) -> None:
        config = MagicMock(daemon_port=60887)

        result = runner.invoke(stop, ["--docker"], obj=_cli_runtime(config))

        assert result.exit_code == 1
        assert "Stopping Docker containers" in result.output
        _services.assert_called_once_with(Path("/fake"))


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------
class TestStatusCommand:
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.get_service_status", return_value={"running": False})
    @patch("gobby.cli.daemon.format_status_message", return_value="Not running")
    def test_status_no_pid_file(
        self,
        _fmt: MagicMock,
        _svc: MagicMock,
        mock_home: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        config = MagicMock()
        config.logging.dir = str(tmp_path)
        result = runner.invoke(status, [], obj=_cli_runtime(config), catch_exceptions=False)
        assert result.exit_code == 0
        assert "Not running" in result.output
        _fmt.assert_called_once()

    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.get_service_status", return_value={"running": False})
    @patch("gobby.cli.daemon.format_status_message", return_value="Not running")
    def test_status_invalid_pid_file(
        self,
        _fmt: MagicMock,
        _svc: MagicMock,
        mock_home: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        (tmp_path / "gobby.pid").write_text("not-a-number")
        config = MagicMock()
        config.logging.dir = str(tmp_path)
        result = runner.invoke(status, [], obj=_cli_runtime(config), catch_exceptions=False)
        assert result.exit_code == 0
        assert "Not running" in result.output
        _fmt.assert_called_once()

    @patch("gobby.utils.deps.check_config_mismatches", return_value=[])
    @patch(
        "gobby.utils.deps.collect_all_deps",
        return_value={"gobby": {}, "coding_clis": {}, "dependencies": {}},
    )
    @patch("gobby.cli.daemon.asyncio.run", return_value={})
    @patch("gobby.cli.daemon.fetch_rich_status", new_callable=MagicMock)
    @patch("gobby.cli.daemon.format_status_message", return_value="Running PID 123")
    @patch("gobby.cli.daemon.format_uptime", return_value="1h 30m")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.daemon.os.kill")
    @patch("gobby.cli.daemon.probe_daemon_lock")
    @patch("gobby.cli.daemon.get_gobby_home")
    def test_status_reports_http_unavailable_when_both_probes_fail(
        self,
        mock_home: MagicMock,
        mock_probe: MagicMock,
        mock_kill: MagicMock,
        mock_process: MagicMock,
        _uptime: MagicMock,
        _fmt: MagicMock,
        _fetch: MagicMock,
        _async: MagicMock,
        _deps: MagicMock,
        _mismatches: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        mock_home.return_value = tmp_path
        mock_probe.return_value = SingletonProbe(state=ProbeState.DAEMON, pid=12345, role="daemon")
        (tmp_path / "gobby.pid").write_text("12345")
        mock_kill.return_value = None
        mock_process.return_value.create_time.return_value = 0.0
        _async.return_value = RichStatusProbe(
            status_failure=EndpointProbeFailure(
                endpoint="/api/admin/status",
                error_class="ReadTimeout",
                detail="rich status deadline",
            ),
            health_failure=EndpointProbeFailure(
                endpoint="/api/health",
                error_class="ConnectError",
                detail="connection refused",
            ),
        )

        config = MagicMock()
        config.logging.dir = str(tmp_path)
        config.daemon_port = 60888
        config.websocket.port = 60889
        config.ui.enabled = False
        bootstrap_path = tmp_path / "bootstrap.yaml"
        files_home = tmp_path / "files"
        files_home.mkdir()
        bootstrap_path.write_text(
            f"daemon_port: 61999\nwebsocket_port: 62000\nfiles_home: {files_home}\n",
            encoding="utf-8",
        )
        bootstrap_path.chmod(0o600)
        runtime = CliRuntime(config_file=str(bootstrap_path), config=config)
        runtime._database = MagicMock()

        result = runner.invoke(status, [], obj=runtime, catch_exceptions=False)
        assert result.exit_code == 0
        assert "Running PID 123" in result.output
        assert _fmt.call_args.kwargs["control_plane_error"] == (
            "endpoint /api/admin/status failed with ReadTimeout "
            "(rich status deadline); endpoint /api/health failed with ConnectError "
            "(connection refused); PID 12345"
        )
        assert _fmt.call_args.kwargs["status_details_error"] is None

    @patch("gobby.utils.deps.check_config_mismatches", return_value=[])
    @patch("gobby.utils.deps.collect_all_deps", side_effect=RuntimeError("database unavailable"))
    @patch("gobby.cli.daemon.asyncio.run", return_value={})
    @patch("gobby.cli.daemon.fetch_rich_status", new_callable=MagicMock)
    @patch("gobby.cli.daemon.format_status_message", return_value="Running PID 123")
    @patch("gobby.cli.daemon.format_uptime", return_value="1h 30m")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.daemon.os.kill")
    @patch("gobby.cli.daemon.probe_daemon_lock")
    @patch("gobby.cli.daemon.get_gobby_home")
    def test_status_degrades_dependency_probe_without_losing_daemon_health(
        self,
        mock_home: MagicMock,
        mock_probe: MagicMock,
        mock_kill: MagicMock,
        mock_process: MagicMock,
        _uptime: MagicMock,
        mock_format: MagicMock,
        _fetch: MagicMock,
        _async: MagicMock,
        _deps: MagicMock,
        _mismatches: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        mock_home.return_value = tmp_path
        mock_probe.return_value = SingletonProbe(state=ProbeState.DAEMON, pid=12345, role="daemon")
        (tmp_path / "gobby.pid").write_text("12345")
        mock_kill.return_value = None
        mock_process.return_value.create_time.return_value = 0.0
        _async.return_value = RichStatusProbe(
            api_data={"process": {}},
            health_confirmed=True,
        )

        config = MagicMock()
        config.logging.dir = str(tmp_path)
        config.daemon_port = 60888
        config.websocket.port = 60889
        config.ui.enabled = False

        result = runner.invoke(status, [], obj=_cli_runtime(config), catch_exceptions=False)

        assert result.exit_code == 0
        assert "Running PID 123" in result.output
        assert mock_format.call_args.kwargs["api_data"] == {"process": {}}
        assert mock_format.call_args.kwargs["control_plane_error"] is None
        assert mock_format.call_args.kwargs["status_details_error"] is None
        assert mock_format.call_args.kwargs["deps_info"] == {
            "dependencies": {
                "required": {
                    "status": {
                        "state": "invalid",
                        "installed_version": None,
                        "minimum_version": None,
                        "expected_version": None,
                        "path": None,
                        "error": "Dependency status collection failed: RuntimeError",
                    }
                },
                "optional": {},
            },
            "integrations": {
                "embeddings_provider": {
                    "status": "degraded",
                    "error": "RuntimeError",
                }
            },
        }

    @patch("gobby.utils.deps.check_config_mismatches", return_value=[])
    @patch(
        "gobby.utils.deps.collect_all_deps",
        return_value={"gobby": {}, "coding_clis": {}, "dependencies": {}},
    )
    @patch("gobby.cli.daemon.asyncio.run")
    @patch("gobby.cli.daemon.fetch_rich_status", new_callable=MagicMock)
    @patch("gobby.cli.daemon.format_status_message", return_value="Running PID 123")
    @patch("gobby.cli.daemon.format_uptime", return_value="1h 30m")
    @patch("gobby.cli.daemon.psutil.Process")
    @patch("gobby.cli.daemon.os.kill")
    @patch("gobby.cli.daemon.probe_daemon_lock")
    @patch("gobby.cli.daemon.get_gobby_home")
    @patch("gobby.cli.daemon.get_port_listener_pid", return_value=None)
    def test_status_timeout_with_healthy_fallback_keeps_daemon_running(
        self,
        _listener: MagicMock,
        mock_home: MagicMock,
        mock_probe: MagicMock,
        mock_kill: MagicMock,
        mock_process: MagicMock,
        _uptime: MagicMock,
        mock_format: MagicMock,
        _fetch: MagicMock,
        mock_async: MagicMock,
        _deps: MagicMock,
        _mismatches: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        mock_home.return_value = tmp_path
        mock_probe.return_value = SingletonProbe(state=ProbeState.DAEMON, pid=12345, role="daemon")
        (tmp_path / "gobby.pid").write_text("12345")
        mock_kill.return_value = None
        mock_process.return_value.create_time.return_value = 0.0
        mock_async.return_value = RichStatusProbe(
            status_failure=EndpointProbeFailure(
                endpoint="/api/admin/status",
                error_class="ReadTimeout",
                detail="rich status deadline",
            ),
            health_confirmed=True,
        )

        config = MagicMock()
        config.logging.dir = str(tmp_path)
        config.daemon_port = 60888
        config.websocket.port = 60889
        config.ui.enabled = False

        result = runner.invoke(status, [], obj=_cli_runtime(config), catch_exceptions=False)

        assert result.exit_code == 0
        assert "Running PID 123" in result.output
        assert mock_format.call_args.kwargs["control_plane_error"] is None
        assert mock_format.call_args.kwargs["status_details_error"] == (
            "temporarily unavailable; endpoint /api/admin/status failed with ReadTimeout "
            "(rich status deadline); fallback /api/health is healthy; PID 12345"
        )

    @patch("gobby.cli.daemon.format_status_message", return_value="Stale PID")
    @patch("gobby.cli.daemon.os.kill", side_effect=ProcessLookupError)
    @patch("gobby.cli.daemon.get_gobby_home")
    def test_status_stale_pid(
        self,
        mock_home: MagicMock,
        _kill: MagicMock,
        _fmt: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        mock_home.return_value = tmp_path
        (tmp_path / "gobby.pid").write_text("99999")
        config = MagicMock()
        config.logging.dir = str(tmp_path)
        result = runner.invoke(status, [], obj=_cli_runtime(config), catch_exceptions=False)
        assert result.exit_code == 0
        assert "Stale" in result.output

    @patch("gobby.cli.runtime.get_cli_runtime")
    @patch("gobby.cli.daemon.get_gobby_home")
    def test_status_reports_maintenance_without_runtime(
        self,
        mock_home: MagicMock,
        mock_runtime: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from gobby.runner_pid_file import claim_pid_file

        mock_home.return_value = tmp_path
        claim = claim_pid_file(tmp_path / "gobby.pid", role="maintenance")
        assert claim is not None
        try:
            result = runner.invoke(status, [], catch_exceptions=False)
        finally:
            claim.release()
        assert result.exit_code == 0
        assert "maintenance" in result.output.lower()
        mock_runtime.assert_not_called()

    @patch("gobby.cli.runtime.get_cli_runtime")
    @patch("gobby.cli.daemon.probe_daemon_lock")
    @patch("gobby.cli.daemon.get_gobby_home")
    def test_status_reports_typed_non_daemon_states(
        self,
        mock_home: MagicMock,
        mock_probe: MagicMock,
        mock_runtime: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        mock_home.return_value = tmp_path
        # Each state must answer the operator, so they are pinned to distinct
        # wording rather than to the raw ProbeState name they used to echo.
        cases = {
            ProbeState.LIVE_RESERVATION: "starting",
            ProbeState.STALE_RESERVATION: "an earlier start did not finish",
            ProbeState.TRANSITIONING: "transitioning",
        }
        for state, expected in cases.items():
            mock_probe.return_value = SingletonProbe(state=state)
            result = runner.invoke(status, [], catch_exceptions=False)
            assert result.exit_code == 0
            assert expected in result.output.lower()
        mock_probe.return_value = SingletonProbe(state=ProbeState.ABSENT)
        result = runner.invoke(status, [], catch_exceptions=False)
        assert result.exit_code == 0
        assert "stopped" in result.output.lower() or "not running" in result.output.lower()
        mock_runtime.assert_not_called()


class TestHealthCommand:
    @patch("gobby.cli.daemon_health.httpx.get")
    @patch("gobby.cli.daemon_health._is_process_alive", return_value=True)
    @patch("gobby.cli.daemon_health.probe_daemon_lock")
    @patch("gobby.cli.daemon_health.get_gobby_home")
    def test_health_surfaces_hook_runtime_degradation(
        self,
        mock_home: MagicMock,
        mock_probe: MagicMock,
        _alive: MagicMock,
        mock_get: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from gobby.runner_pid_file import ProbeState, SingletonProbe

        mock_home.return_value = tmp_path
        mock_probe.return_value = SingletonProbe(state=ProbeState.DAEMON, pid=12345, role="daemon")
        (tmp_path / "gobby.pid").write_text("12345")
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "status": "degraded",
            "hook_runtime": {
                "state": "schema_mismatch",
                "detail": "ghook envelope schema 2 does not match daemon schema 1.",
            },
        }
        config = MagicMock(daemon_port=60887)

        result = runner.invoke(health, [], obj=_cli_runtime(config), catch_exceptions=False)

        assert result.exit_code == 1
        assert "Gobby daemon: degraded" in result.output
        assert "hook runtime: schema_mismatch" in result.output

    @patch("gobby.cli.runtime.get_cli_runtime")
    @patch("gobby.cli.daemon_health.httpx.get")
    @patch("gobby.cli.daemon_health.get_gobby_home")
    def test_health_reports_maintenance_without_runtime_or_http(
        self,
        mock_home: MagicMock,
        mock_http: MagicMock,
        mock_runtime: MagicMock,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        from gobby.runner_pid_file import claim_pid_file

        mock_home.return_value = tmp_path
        claim = claim_pid_file(tmp_path / "gobby.pid", role="maintenance")
        assert claim is not None
        try:
            result = runner.invoke(health, [], catch_exceptions=False)
        finally:
            claim.release()
        assert result.exit_code == 1
        assert "maintenance" in result.output.lower()
        mock_runtime.assert_not_called()
        mock_http.assert_not_called()
