"""Tests for --neo4j flag in CLI and Neo4j service utilities."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.utils.status import format_status_message

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# services.py — is_neo4j_installed / get_neo4j_status
# ---------------------------------------------------------------------------


class TestIsNeo4jInstalled:
    """Tests for is_neo4j_installed."""

    def test_returns_true_when_service_dir_exists(self, tmp_path: Path) -> None:
        from gobby.cli.services import is_neo4j_installed

        svc_dir = tmp_path / "services" / "neo4j"
        svc_dir.mkdir(parents=True)

        assert is_neo4j_installed(gobby_home=tmp_path) is True

    def test_returns_false_when_service_dir_missing(self, tmp_path: Path) -> None:
        from gobby.cli.services import is_neo4j_installed

        assert is_neo4j_installed(gobby_home=tmp_path) is False


class TestGetNeo4jStatus:
    """Tests for get_neo4j_status."""

    @pytest.mark.asyncio
    async def test_returns_status_dict(self, tmp_path: Path) -> None:
        from gobby.cli.services import get_neo4j_status

        svc_dir = tmp_path / "services" / "neo4j"
        svc_dir.mkdir(parents=True)

        with patch("gobby.cli.services.is_neo4j_healthy", return_value=False):
            result = await get_neo4j_status(
                gobby_home=tmp_path,
                neo4j_url="http://localhost:8474",
            )

        assert result["installed"] is True
        assert result["healthy"] is False
        assert result["url"] == "http://localhost:8474"

    @pytest.mark.asyncio
    async def test_returns_not_installed(self, tmp_path: Path) -> None:
        from gobby.cli.services import get_neo4j_status

        with patch("gobby.cli.services.is_neo4j_healthy", return_value=False):
            result = await get_neo4j_status(gobby_home=tmp_path)

        assert result["installed"] is False


# ---------------------------------------------------------------------------
# install.py — --neo4j flag
# ---------------------------------------------------------------------------


class TestInstallNeo4jFlag:
    """Tests for Neo4j-related params in install/uninstall commands."""

    def test_install_command_has_neo4j_password(self) -> None:
        """install command has --neo4j-password option."""
        from gobby.cli.install import install

        param_names = [p.name for p in install.params]
        assert "neo4j_password" in param_names

    def test_uninstall_command_has_neo4j_option(self) -> None:
        """uninstall command has --neo4j flag."""
        from gobby.cli.install import uninstall

        param_names = [p.name for p in uninstall.params]
        assert "neo4j_flag" in param_names


# ---------------------------------------------------------------------------
# daemon.py — --docker flag (consolidated from --neo4j)
# ---------------------------------------------------------------------------


class TestDaemonDockerFlag:
    """Tests for --docker flag on daemon start/stop/restart.

    Neo4j service management was consolidated into unified Docker
    service management via the --docker flag.
    """

    def test_start_has_docker_flag(self) -> None:
        from gobby.cli.daemon import start

        param_names = [p.name for p in start.params]
        assert "docker_flag" in param_names

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
        (svc_dir / "docker-compose.yml").write_text("services: {}")

        with (
            patch("gobby.cli.daemon.subprocess.run") as mock_run,
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("gobby.config.app.load_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(
                databases=MagicMock(
                    neo4j=MagicMock(url="http://localhost:8474", auth="neo4j:password"),
                    qdrant=MagicMock(url=None),
                ),
            )
            mock_run.return_value = MagicMock(returncode=0)
            _services_start(tmp_path)

        compose_calls = [c for c in mock_run.call_args_list if "up" in str(c)]
        assert len(compose_calls) >= 1
        assert mock_run.call_count >= 1

    def test_services_start_skips_when_no_docker(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_start

        with patch("shutil.which", return_value=None):
            with patch("gobby.cli.daemon.subprocess.run") as mock_run:
                _services_start(tmp_path)

        mock_run.assert_not_called()
        assert mock_run.call_count == 0
        assert not mock_run.called

    def test_services_stop_runs_compose_down(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_stop

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        (svc_dir / "docker-compose.yml").write_text("services: {}")

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("gobby.cli.daemon.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _services_stop(tmp_path)

        compose_calls = [c for c in mock_run.call_args_list if "down" in str(c)]
        assert len(compose_calls) >= 1

    def test_services_stop_skips_when_no_docker(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_stop

        with patch("shutil.which", return_value=None):
            with patch("gobby.cli.daemon.subprocess.run") as mock_run:
                _services_stop(tmp_path)

        mock_run.assert_not_called()
        assert mock_run.call_count == 0
        assert not mock_run.called


# ---------------------------------------------------------------------------
# status display — Neo4j knowledge graph
# ---------------------------------------------------------------------------


class TestStatusNeo4jDisplay:
    """Tests for Neo4j status in format_status_message."""

    def test_status_shows_neo4j_installed_healthy(self) -> None:
        msg = format_status_message(
            running=True,
            pid=123,
            api_data={
                "memory": {
                    "neo4j": {
                        "configured": True,
                        "installed": True,
                        "healthy": True,
                        "url": "http://localhost:8474",
                    }
                }
            },
        )
        assert "Neo4j" in msg
        assert "healthy" in msg
        assert "localhost:8474" in msg

    def test_status_shows_neo4j_installed_unhealthy(self) -> None:
        msg = format_status_message(
            running=True,
            pid=123,
            api_data={
                "memory": {
                    "neo4j": {
                        "configured": True,
                        "installed": True,
                        "healthy": False,
                        "url": "http://localhost:8474",
                    }
                }
            },
        )
        assert "Neo4j" in msg
        assert "not responding" in msg

    def test_status_shows_neo4j_not_installed(self) -> None:
        msg = format_status_message(
            running=True,
            pid=123,
            api_data={
                "memory": {
                    "neo4j": {
                        "configured": True,
                        "installed": False,
                        "healthy": False,
                    }
                }
            },
        )
        assert "Neo4j" in msg
        assert "not installed" in msg

    def test_status_omits_neo4j_when_no_data(self) -> None:
        msg = format_status_message(running=True, pid=123)
        assert "Neo4j" not in msg
