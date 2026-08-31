"""Tests for FalkorDB CLI flags and status surfaces."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.installers.compose_env import ComposeEnvironmentError, ComposeRuntime
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.status import format_status_message

pytestmark = pytest.mark.unit


class TestGetFalkorDBStatus:
    """Tests for get_falkordb_status."""

    @pytest.mark.asyncio
    async def test_returns_status_dict(self, hub_db: HubDatabase) -> None:
        from gobby.cli.services import get_falkordb_status

        ConfigMutations(hub_db).patch_internal(
            expected_revision=0,
            patch=ConfigPatch(
                values={
                    "databases.falkordb.host": "127.0.0.1",
                    "databases.falkordb.port": 16379,
                },
                secrets={"databases.falkordb.password": SecretUpdate("secret")},
            ),
            source="test",
        )
        with patch("gobby.cli.services.is_falkordb_healthy", return_value=False):
            result = await get_falkordb_status(
                db=hub_db,
                host="127.0.0.1",
                port=16379,
                password="secret",
            )

        assert result["installed"] is True
        assert result["healthy"] is False
        assert result["url"] == "redis://127.0.0.1:16379"

    @pytest.mark.asyncio
    async def test_returns_not_installed(self, hub_db: HubDatabase) -> None:
        from gobby.cli.services import get_falkordb_status

        result = await get_falkordb_status(db=hub_db)

        assert result["installed"] is False


class TestInstallFalkorDBFlags:
    """Tests for FalkorDB-related params in install/uninstall commands."""

    def test_install_command_has_no_falkordb_options(self) -> None:
        """54b9a969c (#19373) made FalkorDB part of the required install stack.

        There is no opt-in `--falkordb` flag and no password override: the
        password is generated, and no install path may reintroduce either.
        """
        from gobby.cli.install import install

        param_names = [p.name for p in install.params]
        assert "falkordb_flag" not in param_names
        assert "falkordb_password_stdin" not in param_names
        assert "components" in param_names

    def test_uninstall_command_takes_components_not_service_flags(self) -> None:
        from gobby.cli.uninstall import uninstall

        param_names = [p.name for p in uninstall.params]
        assert "falkordb_flag" not in param_names
        assert "components" in param_names


class TestDaemonDockerFlag:
    """Tests for managed service lifecycle flags and helpers."""

    def test_start_does_not_have_docker_flag(self) -> None:
        from gobby.cli.daemon import start

        param_names = [p.name for p in start.params]
        assert "docker_flag" not in param_names

    def test_stop_has_docker_flag(self) -> None:
        from gobby.cli.daemon import stop

        param_names = [p.name for p in stop.params]
        assert "docker_flag" in param_names

    def test_restart_has_docker_flag(self) -> None:
        from gobby.cli.daemon import restart

        param_names = [p.name for p in restart.params]
        assert "docker_flag" in param_names

    def test_services_start_runs_compose_up(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_start

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        (svc_dir / "docker-compose.yml").write_text(
            "services:\n"
            "  postgres:\n"
            "    profiles: [postgres]\n"
            "  qdrant:\n"
            "    profiles: [qdrant]\n"
            "  falkordb:\n"
            "    profiles: [falkordb]\n"
        )

        with (
            patch("gobby.cli.daemon.subprocess.run") as mock_run,
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("gobby.cli.datastores.apply_hub_schema_contract"),
            patch(
                "gobby.cli.daemon.resolve_compose_runtime",
                return_value=ComposeRuntime(
                    environment={"GOBBY_FALKORDB_PASSWORD": "password"},
                    profiles=("postgres", "qdrant", "falkordb"),
                ),
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            result = _services_start(tmp_path)

        compose_calls = [call for call in mock_run.call_args_list if "up" in str(call)]
        assert result.outcome == "success"
        assert compose_calls

    def test_services_start_fails_when_no_docker(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_start

        with patch("shutil.which", return_value=None):
            with patch("gobby.cli.daemon.subprocess.run") as mock_run:
                result = _services_start(tmp_path)

        assert result.outcome == "failed"
        mock_run.assert_not_called()

    def test_services_start_skips_when_config_unavailable(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_start

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        (svc_dir / "docker-compose.yml").write_text(
            "services:\n"
            "  postgres:\n"
            "    profiles: [postgres]\n"
            "  qdrant:\n"
            "    profiles: [qdrant]\n"
            "  falkordb:\n"
            "    profiles: [falkordb]\n",
            encoding="utf-8",
        )

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch(
                "gobby.cli.daemon.resolve_compose_runtime",
                side_effect=ComposeEnvironmentError("db"),
            ),
            patch("gobby.cli.daemon.subprocess.run") as mock_run,
        ):
            result = _services_start(tmp_path)

        assert result.outcome == "failed"
        mock_run.assert_not_called()

    def test_services_stop_runs_compose_stop(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_stop

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        (svc_dir / "docker-compose.yml").write_text("services: {}")

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("gobby.cli.daemon.subprocess.run") as mock_run,
            patch(
                "gobby.cli.daemon.resolve_compose_runtime",
                return_value=ComposeRuntime(environment={"PATH": "test"}, profiles=()),
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _services_stop(tmp_path)

        compose_calls = [call for call in mock_run.call_args_list if "stop" in str(call)]
        assert compose_calls
        command = mock_run.call_args.args[0]
        assert command[-1] == "stop"
        assert "down" not in command
        for profile in ("postgres", "qdrant", "falkordb"):
            assert any(
                command[index : index + 2] == ["--profile", profile]
                for index in range(len(command) - 1)
            )
        assert mock_run.call_args.kwargs["cwd"] == str(svc_dir)
        assert mock_run.call_args.kwargs["env"] == {"PATH": "test"}

    def test_services_stop_skips_when_no_docker(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from gobby.cli.daemon import _services_stop

        with patch("shutil.which", return_value=None):
            with patch("gobby.cli.daemon.subprocess.run") as mock_run:
                _services_stop(tmp_path)

        assert caplog.records == []
        mock_run.assert_not_called()


class TestStatusFalkorDBDisplay:
    """Tests for FalkorDB status in format_status_message."""

    def test_status_shows_falkordb_installed_healthy(self) -> None:
        msg = format_status_message(
            running=True,
            pid=123,
            api_data={
                "memory": {
                    "falkordb": {
                        "configured": True,
                        "installed": True,
                        "healthy": True,
                        "url": "redis://127.0.0.1:16379",
                    }
                }
            },
        )
        assert "FalkorDB" in msg
        assert "healthy" in msg
        assert "127.0.0.1:16379" in msg

    def test_status_shows_falkordb_installed_unhealthy(self) -> None:
        msg = format_status_message(
            running=True,
            pid=123,
            api_data={
                "memory": {
                    "falkordb": {
                        "configured": True,
                        "installed": True,
                        "healthy": False,
                        "url": "redis://127.0.0.1:16379",
                    }
                }
            },
        )
        assert "FalkorDB" in msg
        assert "not responding" in msg

    def test_status_shows_falkordb_not_installed(self) -> None:
        msg = format_status_message(
            running=True,
            pid=123,
            api_data={
                "memory": {
                    "falkordb": {
                        "configured": True,
                        "installed": False,
                        "healthy": False,
                    }
                }
            },
        )
        assert "FalkorDB" in msg
        assert "not installed" in msg

    def test_status_omits_falkordb_when_no_data(self) -> None:
        msg = format_status_message(running=True, pid=123)
        assert "FalkorDB" not in msg
