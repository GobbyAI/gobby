"""Tests for FalkorDB installer."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

pytestmark = [pytest.mark.unit]


class TestDockerComposeFalkorDB:
    """Tests for the FalkorDB service in the unified compose template."""

    def test_compose_file_exists(self) -> None:
        from gobby.cli.installers.falkor import _COMPOSE_SRC

        assert _COMPOSE_SRC.exists(), f"Expected {_COMPOSE_SRC} to exist"

    def test_compose_file_is_valid_yaml(self) -> None:
        from gobby.cli.installers.falkor import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert isinstance(data, dict)

    def test_compose_has_falkordb_service(self) -> None:
        from gobby.cli.installers.falkor import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "falkordb" in data["services"]

    def test_compose_falkordb_ports(self) -> None:
        from gobby.cli.installers.falkor import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        ports = data["services"]["falkordb"]["ports"]
        assert "${GOBBY_FALKORDB_PORT:-16379}:6379" in ports
        assert "${GOBBY_FALKORDB_BROWSER_PORT:-13000}:3000" in ports

    def test_compose_has_data_volume(self) -> None:
        from gobby.cli.installers.falkor import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "gobby_falkordb_data" in data.get("volumes", {})
        assert data["volumes"]["gobby_falkordb_data"]["name"] == "gobby_falkordb_data"

    def test_compose_has_healthcheck(self) -> None:
        from gobby.cli.installers.falkor import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        assert "healthcheck" in data["services"]["falkordb"]

    def test_compose_falkordb_has_profiles(self) -> None:
        from gobby.cli.installers.falkor import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        profiles = data["services"]["falkordb"]["profiles"]
        assert "falkordb" in profiles
        assert "all" in profiles

    def test_compose_falkordb_image(self) -> None:
        from gobby.cli.installers.falkor import _COMPOSE_SRC

        data = yaml.safe_load(_COMPOSE_SRC.read_text())
        image = data["services"]["falkordb"]["image"]
        assert image == "falkordb/falkordb:latest"


class TestInstallFalkorDB:
    """Tests for install_falkordb."""

    def test_install_falkordb_no_docker(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import install_falkordb

        with patch.object(shutil, "which", return_value=None):
            result = install_falkordb(gobby_home=tmp_path)

        assert result["success"] is False
        assert "Docker" in result["error"]

    def test_install_falkordb_invalid_password_raises_usage_value_error(
        self, tmp_path: Path
    ) -> None:
        from gobby.cli.installers.falkor import install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch(
                "gobby.cli.installers.falkor._resolve_falkordb_password",
                side_effect=ValueError("FalkorDB password must not contain whitespace"),
            ),
        ):
            with pytest.raises(ValueError, match="password must not contain whitespace"):
                install_falkordb(gobby_home=tmp_path, password="has space")

    def test_install_falkordb_copies_compose_file(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import ResolvedFalkorPassword, install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess") as mock_subprocess,
            patch(
                "gobby.cli.installers.falkor._resolve_falkordb_password",
                return_value=ResolvedFalkorPassword("password123", "generated", True),
            ),
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
            patch("gobby.cli.installers.falkor._update_config"),
            patch("gobby.cli.installers.falkor._write_bootstrap_password", return_value=True),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            result = install_falkordb(gobby_home=tmp_path)

        assert result["success"] is True
        assert result["url"] == "redis://127.0.0.1:16379"
        assert result["password"] == "password123"
        assert (tmp_path / "services" / "docker-compose.yml").exists()

    def test_install_falkordb_calls_docker_compose_with_profile(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import ResolvedFalkorPassword, install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess") as mock_subprocess,
            patch(
                "gobby.cli.installers.falkor._resolve_falkordb_password",
                return_value=ResolvedFalkorPassword("password123", "provided", False),
            ),
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
            patch("gobby.cli.installers.falkor._update_config"),
            patch("gobby.cli.installers.falkor._write_bootstrap_password", return_value=True),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            install_falkordb(gobby_home=tmp_path)

        call_args = mock_subprocess.run.call_args
        assert call_args is not None
        cmd = call_args[0][0]
        assert "docker" in cmd
        assert "--profile" in cmd
        assert "falkordb" in cmd
        assert "up" in cmd
        assert "-d" in cmd
        assert call_args.kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "password123"

    def test_install_falkordb_updates_config(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import ResolvedFalkorPassword, install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess") as mock_subprocess,
            patch(
                "gobby.cli.installers.falkor._resolve_falkordb_password",
                return_value=ResolvedFalkorPassword("password123", "provided", False),
            ),
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
            patch("gobby.cli.installers.falkor._update_config") as mock_update,
            patch("gobby.cli.installers.falkor._write_bootstrap_password", return_value=True),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            install_falkordb(gobby_home=tmp_path)

        mock_update.assert_called_once_with(
            host="127.0.0.1",
            port=16379,
            password="password123",
            gobby_home=tmp_path,
        )

    def test_update_config_removes_legacy_neo4j_keys(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import _update_config
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.hub.protocol import HubDatabase
        from tests.fixtures.migrations import run_migrations

        db_path = tmp_path / "hub-postgres.db"

        def open_db(_home: Path, *, apply_migrations: bool = True) -> HubDatabase:
            _ = apply_migrations
            return HubDatabase(db_path)

        db = HubDatabase(db_path)
        try:
            run_migrations(db)
            store = ConfigStore(db)
            store.set("databases.neo4j.url", "http://localhost:8474")
            store.set("databases.neo4j.password", "$secret:password")
        finally:
            db.close()

        with patch("gobby.cli.installers.falkor._open_config_db", side_effect=open_db):
            _update_config(
                host="127.0.0.1",
                port=16379,
                password="password123",
                gobby_home=tmp_path,
            )

        db = HubDatabase(db_path)
        try:
            store = ConfigStore(db)
            assert store.get("databases.neo4j.url") is None
            assert store.get("databases.neo4j.password") is None
            assert store.get("databases.falkordb.host") == "127.0.0.1"
        finally:
            db.close()

    def test_install_falkordb_returns_error_on_compose_failure(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import ResolvedFalkorPassword, install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess") as mock_subprocess,
            patch(
                "gobby.cli.installers.falkor._resolve_falkordb_password",
                return_value=ResolvedFalkorPassword("password123", "provided", False),
            ),
        ):
            mock_subprocess.run.return_value = MagicMock(
                returncode=1, stderr="container failed", stdout=""
            )
            mock_subprocess.TimeoutExpired = TimeoutError

            result = install_falkordb(gobby_home=tmp_path)

        assert result["success"] is False
        assert "compose up failed" in result["error"]


class TestUninstallFalkorDB:
    """Tests for uninstall_falkordb."""

    def test_uninstall_falkordb_not_installed(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import uninstall_falkordb

        with (
            patch("gobby.cli.installers.falkor._clear_config") as mock_clear_config,
            patch("gobby.cli.installers.falkor._clear_bootstrap_password") as mock_clear_bootstrap,
        ):
            result = uninstall_falkordb(gobby_home=tmp_path)

        assert result == {"success": True, "data_removed": False}
        mock_clear_config.assert_called_once_with(gobby_home=tmp_path)
        mock_clear_bootstrap.assert_called_once_with(tmp_path)

    def test_uninstall_falkordb_runs_compose_down_with_profile(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import uninstall_falkordb

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        (svc_dir / "docker-compose.yml").write_text("services: {}")

        with (
            patch("gobby.cli.installers.falkor.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.falkor._clear_config"),
            patch("gobby.cli.installers.falkor._clear_bootstrap_password"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            result = uninstall_falkordb(gobby_home=tmp_path)

        assert result["success"] is True
        call_args = mock_subprocess.run.call_args
        assert call_args is not None
        cmd = call_args[0][0]
        assert "--profile" in cmd
        assert "falkordb" in cmd
        assert "down" in cmd

    def test_uninstall_falkordb_with_volume_purge(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import uninstall_falkordb

        svc_dir = tmp_path / "services"
        svc_dir.mkdir(parents=True)
        (svc_dir / "docker-compose.yml").write_text("services: {}")

        with (
            patch("gobby.cli.installers.falkor.subprocess") as mock_subprocess,
            patch("gobby.cli.installers.falkor._clear_config"),
            patch("gobby.cli.installers.falkor._clear_bootstrap_password"),
        ):
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            uninstall_falkordb(gobby_home=tmp_path, purge=True)

        assert mock_subprocess.run.call_count == 2
        volume_cmd = mock_subprocess.run.call_args_list[1][0][0]
        assert volume_cmd == ["docker", "volume", "rm", "gobby_falkordb_data"]
