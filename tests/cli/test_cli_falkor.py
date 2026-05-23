"""Tests for FalkorDB CLI flags and status surfaces."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.utils.status import format_status_message

pytestmark = pytest.mark.unit


def _make_config_db(path: Path) -> LocalDatabase:
    db = LocalDatabase(path)
    run_migrations(db)
    return db


class TestIsFalkorDBInstalled:
    """Tests for is_falkordb_installed."""

    def test_returns_true_when_config_store_keys_exist(self, tmp_path: Path) -> None:
        from gobby.cli.services import is_falkordb_installed

        db = _make_config_db(tmp_path / "gobby-hub.db")
        try:
            store = ConfigStore(db)
            store.set("databases.falkordb.host", "127.0.0.1")
            store.set("databases.falkordb.port", 16379)
            assert is_falkordb_installed(db=db) is True
        finally:
            db.close()

    def test_returns_false_when_config_store_keys_are_missing(self, tmp_path: Path) -> None:
        from gobby.cli.services import is_falkordb_installed

        db = _make_config_db(tmp_path / "gobby-hub.db")
        try:
            assert is_falkordb_installed(db=db) is False
        finally:
            db.close()


class TestGetFalkorDBStatus:
    """Tests for get_falkordb_status."""

    @pytest.mark.asyncio
    async def test_returns_status_dict(self, tmp_path: Path) -> None:
        from gobby.cli.services import get_falkordb_status

        db = _make_config_db(tmp_path / "gobby-hub.db")
        try:
            store = ConfigStore(db)
            store.set("databases.falkordb.host", "127.0.0.1")
            store.set("databases.falkordb.port", 16379)
            with patch("gobby.cli.services.is_falkordb_healthy", return_value=False):
                result = await get_falkordb_status(
                    db=db,
                    host="127.0.0.1",
                    port=16379,
                    password="secret",
                )
        finally:
            db.close()

        assert result["installed"] is True
        assert result["healthy"] is False
        assert result["url"] == "redis://127.0.0.1:16379"

    @pytest.mark.asyncio
    async def test_returns_not_installed(self, tmp_path: Path) -> None:
        from gobby.cli.services import get_falkordb_status

        db = _make_config_db(tmp_path / "gobby-hub.db")
        try:
            result = await get_falkordb_status(db=db)
        finally:
            db.close()

        assert result["installed"] is False


class TestInstallFalkorDBFlags:
    """Tests for FalkorDB-related params in install/uninstall commands."""

    def test_install_command_has_falkordb_options(self) -> None:
        from gobby.cli.install import install

        param_names = [p.name for p in install.params]
        assert "falkordb_flag" in param_names
        assert "falkordb_password" in param_names

    def test_uninstall_command_has_falkordb_option(self) -> None:
        from gobby.cli.install import uninstall

        param_names = [p.name for p in uninstall.params]
        assert "falkordb_flag" in param_names


class TestDaemonDockerFlag:
    """Tests for --docker flag on daemon start/stop/restart."""

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
            patch("gobby.cli.daemon._open_services_config_db", return_value=MagicMock()),
            patch("gobby.config.app.load_config") as mock_config,
        ):
            mock_config.return_value = MagicMock(
                databases=MagicMock(
                    falkordb=MagicMock(requirepass="password"),
                    qdrant=MagicMock(url=None),
                ),
            )
            mock_run.return_value = MagicMock(returncode=0)
            _services_start(tmp_path)

        compose_calls = [call for call in mock_run.call_args_list if "up" in str(call)]
        assert compose_calls

    def test_services_start_skips_when_no_docker(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_start

        with patch("shutil.which", return_value=None):
            with patch("gobby.cli.daemon.subprocess.run") as mock_run:
                _services_start(tmp_path)

        mock_run.assert_not_called()

    def test_services_start_skips_when_config_unavailable(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_start

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        (svc_dir / "docker-compose.yml").write_text("services: {}", encoding="utf-8")

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch("gobby.cli.daemon._open_services_config_db", side_effect=RuntimeError("db")),
            patch("gobby.cli.daemon.subprocess.run") as mock_run,
        ):
            _services_start(tmp_path)

        mock_run.assert_not_called()

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

        compose_calls = [call for call in mock_run.call_args_list if "down" in str(call)]
        assert compose_calls

    def test_services_stop_skips_when_no_docker(self, tmp_path: Path) -> None:
        from gobby.cli.daemon import _services_stop

        with patch("shutil.which", return_value=None):
            with patch("gobby.cli.daemon.subprocess.run") as mock_run:
                _services_stop(tmp_path)

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
