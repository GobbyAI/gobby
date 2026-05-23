"""Tests for FalkorDB installer."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

pytestmark = [pytest.mark.unit]


class TestDockerComposeFalkorDB:
    """Tests for the FalkorDB service in docker-compose.services.yml."""

    def test_compose_has_falkordb_service(self) -> None:
        from gobby.cli.installers.falkordb import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "falkordb" in data["services"]

    def test_compose_falkordb_ports(self) -> None:
        from gobby.cli.installers.falkordb import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        ports = data["services"]["falkordb"]["ports"]
        assert "16379:6379" in ports
        assert "13000:3000" in ports

    def test_compose_falkordb_uses_password_env(self) -> None:
        from gobby.cli.installers.falkordb import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        env = data["services"]["falkordb"]["environment"]
        assert "REDIS_ARGS=--requirepass ${GOBBY_FALKORDB_PASSWORD}" in env

    def test_compose_falkordb_has_profiles(self) -> None:
        from gobby.cli.installers.falkordb import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        profiles = data["services"]["falkordb"]["profiles"]
        assert "falkordb" in profiles
        assert "all" in profiles

    def test_compose_has_falkordb_volume(self) -> None:
        from gobby.cli.installers.falkordb import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert data["volumes"]["gobby_falkordb_data"]["name"] == "gobby_falkordb_data"


class TestInstallFalkorDB:
    """Tests for install_falkordb."""

    def test_install_falkordb_no_docker(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkordb import install_falkordb

        with patch.object(shutil, "which", return_value=None):
            result = install_falkordb(gobby_home=tmp_path)

        assert result["success"] is False
        assert "Docker" in result["error"]

    def test_install_falkordb_copies_compose_file(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkordb import install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkordb.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkordb._wait_for_redis_ping", return_value=True),
            patch("gobby.cli.installers.falkordb._update_config", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = install_falkordb(gobby_home=tmp_path, password="DirectPass123")

        assert result["success"] is True
        assert (tmp_path / "services" / "docker-compose.yml").exists()

    def test_install_falkordb_runs_compose_with_profile(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkordb import install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkordb.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkordb._wait_for_redis_ping", return_value=True),
            patch("gobby.cli.installers.falkordb._update_config", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            install_falkordb(gobby_home=tmp_path, password="DirectPass123")

        cmd = mock_run.call_args.args[0]
        assert "--profile" in cmd
        assert "falkordb" in cmd
        assert "up" in cmd

    def test_install_falkordb_persists_custom_password(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkordb import install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkordb.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkordb._wait_for_redis_ping", return_value=True),
            patch("gobby.cli.installers.falkordb._update_config", return_value=True) as mock_update,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = install_falkordb(gobby_home=tmp_path, password="DirectPass123")

        assert result["success"] is True
        mock_update.assert_called_once_with("DirectPass123")
        assert mock_run.call_args.kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "DirectPass123"

    def test_install_falkordb_honors_gobby_home_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.cli.installers.falkordb import install_falkordb

        monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkordb.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkordb._wait_for_redis_ping", return_value=True),
            patch("gobby.cli.installers.falkordb._update_config", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = install_falkordb(password="DirectPass123")

        assert result["success"] is True
        assert (tmp_path / "services" / "docker-compose.yml").exists()

    def test_install_falkordb_rejects_invalid_password(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkordb import install_falkordb

        with patch.object(shutil, "which", return_value="/usr/bin/docker"):
            result = install_falkordb(gobby_home=tmp_path, password="has space")

        assert result["success"] is False
        assert "whitespace" in result["error"]


class TestUninstallFalkorDB:
    """Tests for uninstall_falkordb."""

    def test_uninstall_falkordb_not_installed(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkordb import uninstall_falkordb

        with patch("gobby.cli.installers.falkordb._clear_config", return_value=True):
            result = uninstall_falkordb(gobby_home=tmp_path)

        assert result["success"] is True
        assert result["already_uninstalled"] is True

    def test_uninstall_falkordb_runs_compose_down_with_profile(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkordb import uninstall_falkordb

        services = tmp_path / "services"
        services.mkdir()
        (services / "docker-compose.yml").write_text("services: {}\n")

        with (
            patch("gobby.cli.installers.falkordb.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkordb._clear_config", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = uninstall_falkordb(gobby_home=tmp_path)

        assert result["success"] is True
        cmd = mock_run.call_args.args[0]
        assert "--profile" in cmd
        assert "falkordb" in cmd
        assert "down" in cmd
