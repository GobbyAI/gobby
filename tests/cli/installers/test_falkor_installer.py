"""Tests for FalkorDB installer."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.storage.hub.protocol import HubDatabase

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
        assert (
            "${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:${GOBBY_FALKORDB_PORT:-16379}:6379"
        ) in ports
        assert "127.0.0.1:${GOBBY_FALKORDB_BROWSER_PORT:-13000}:3000" in ports

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
            patch("gobby.cli.installers.falkor.resolve_compose_runtime") as resolve,
        ):
            resolve.return_value.environment = {"GOBBY_FALKORDB_PASSWORD": "password123"}
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
                return_value=ResolvedFalkorPassword("password123", "reused", False),
            ),
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
            patch("gobby.cli.installers.falkor._update_config"),
            patch("gobby.cli.installers.falkor.resolve_compose_runtime") as resolve,
        ):
            resolve.return_value.environment = {"GOBBY_FALKORDB_PASSWORD": "password123"}
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
                return_value=ResolvedFalkorPassword("password123", "reused", False),
            ),
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
            patch("gobby.cli.installers.falkor._update_config") as mock_update,
            patch("gobby.cli.installers.falkor.resolve_compose_runtime") as resolve,
        ):
            resolve.return_value.environment = {"GOBBY_FALKORDB_PASSWORD": "password123"}
            mock_subprocess.run.return_value = MagicMock(returncode=0)
            mock_subprocess.TimeoutExpired = TimeoutError

            result = install_falkordb(gobby_home=tmp_path)

        assert result["success"] is True
        assert result["password_source"] == "reused"
        assert mock_subprocess.run.return_value.returncode == 0
        mock_update.assert_called_once_with(
            port=16379,
            password="password123",
            gobby_home=tmp_path,
        )

    def test_update_config_removes_legacy_neo4j_keys(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        from gobby.cli.installers.falkor import _update_config
        from gobby.storage.config_repository import ConfigRepository

        class NonClosingDb:
            dialect = "postgres"

            def __getattr__(self, name: str) -> object:
                return getattr(hub_db, name)

            def close(self) -> None:
                pass

        @contextmanager
        def open_db(_home: Path, *, apply_migrations: bool = True) -> Iterator[NonClosingDb]:
            _ = apply_migrations
            yield NonClosingDb()

        with patch("gobby.cli.installers.falkor._config_db", side_effect=open_db):
            _update_config(
                port=16379,
                password="password123",
                gobby_home=tmp_path,
            )

        snapshot = ConfigRepository(hub_db).read(resolve_secrets=False)
        assert snapshot.values["databases.falkordb.host"] == "127.0.0.1"

    def test_install_falkordb_returns_error_on_compose_failure(self, tmp_path: Path) -> None:
        from gobby.cli.installers.falkor import ResolvedFalkorPassword, install_falkordb

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess") as mock_subprocess,
            patch(
                "gobby.cli.installers.falkor._resolve_falkordb_password",
                return_value=ResolvedFalkorPassword("password123", "reused", False),
            ),
            patch("gobby.cli.installers.falkor._update_config"),
            patch("gobby.cli.installers.falkor.resolve_compose_runtime") as resolve,
        ):
            resolve.return_value.environment = {"GOBBY_FALKORDB_PASSWORD": "password123"}
            mock_subprocess.run.return_value = MagicMock(
                returncode=1, stderr="container failed", stdout=""
            )
            mock_subprocess.TimeoutExpired = TimeoutError

            result = install_falkordb(gobby_home=tmp_path)

        assert result["success"] is False
        assert "compose up failed" in result["error"]
