"""Tests for the FalkorDB installer contract."""

from __future__ import annotations

import importlib
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations

pytestmark = pytest.mark.unit


def _falkor_module():
    return importlib.import_module("gobby.cli.installers.falkor")


def _read_compose_services(repo_root: Path) -> dict[str, object]:
    compose_path = repo_root / "src/gobby/data/docker-compose.services.yml"
    return yaml.safe_load(compose_path.read_text(encoding="utf-8"))


def _new_config_db(path: Path) -> LocalDatabase:
    db = LocalDatabase(path)
    run_migrations(db)
    return db


class TestDockerComposeFalkorDB:
    def test_compose_replaces_neo4j_with_falkordb(self, repo_root: Path) -> None:
        data = _read_compose_services(repo_root)

        services = data["services"]
        volumes = data.get("volumes", {})

        assert "falkordb" in services
        assert "neo4j" not in services
        assert "gobby_falkordb_data" in volumes
        assert "gobby_neo4j_data" not in volumes
        assert "gobby_neo4j_logs" not in volumes

    def test_falkordb_service_has_profile_ports_and_healthcheck(self, repo_root: Path) -> None:
        data = _read_compose_services(repo_root)
        falkordb = data["services"]["falkordb"]

        assert falkordb["image"] == "falkordb/falkordb:latest"
        assert "falkordb" in falkordb["profiles"]
        assert "all" in falkordb["profiles"]
        assert "16379:6379" in falkordb["ports"]
        assert "13000:3000" in falkordb["ports"]
        assert (
            "REDIS_ARGS=--requirepass ${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor}"
            in falkordb["environment"]
        )
        assert "healthcheck" in falkordb
        assert "redis-cli" in " ".join(falkordb["healthcheck"]["test"])


class TestInstallFalkorDB:
    def test_installer_runs_falkordb_compose_profile_and_healthcheck(self, tmp_path: Path) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True) as health,
            patch("gobby.cli.installers.falkor._update_config"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is True
        assert result["falkordb_host"] == "localhost"
        assert result["falkordb_port"] == 6379
        assert (tmp_path / "services" / "docker-compose.yml").exists()

        cmd = mock_run.call_args.args[0]
        assert cmd[:4] == ["docker", "compose", "-f", str(tmp_path / "services/docker-compose.yml")]
        assert "--profile" in cmd
        assert "falkordb" in cmd
        assert "up" in cmd
        assert "-d" in cmd
        assert mock_run.call_args.kwargs["env"]["FALKORDB_ARGS"] == "--requirepass secret"
        health.assert_called_once_with(host="localhost", port=6379, password="secret")

    def test_installer_writes_password_to_bootstrap_and_config_store(self, tmp_path: Path) -> None:
        module = _falkor_module()
        db_path = tmp_path / "gobby-hub.db"
        db = _new_config_db(db_path)
        db.close()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is True

        bootstrap = yaml.safe_load((tmp_path / "bootstrap.yaml").read_text(encoding="utf-8"))
        assert bootstrap["falkordb_password"] == "secret"
        assert "neo4j_password" not in bootstrap

        verify_db = LocalDatabase(db_path)
        try:
            store = ConfigStore(verify_db)
            assert store.get("databases.falkordb.host") == "localhost"
            assert store.get("databases.falkordb.port") == 6379
            assert store.get("databases.falkordb.requirepass") == "$secret:requirepass"
            assert store.get("databases.neo4j.auth") is None

            row = verify_db.fetchone(
                "SELECT value, is_secret FROM config_store WHERE key = ?",
                ("databases.falkordb.requirepass",),
            )
            assert row is not None
            assert json.loads(row["value"]) == "$secret:requirepass"
            assert row["is_secret"] == 1
            assert verify_db.fetchone("SELECT 1 FROM secrets WHERE name = ?", ("requirepass",))
        finally:
            verify_db.close()

    def test_installer_does_not_write_bootstrap_when_config_store_update_fails(
        self, tmp_path: Path
    ) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
            patch(
                "gobby.cli.installers.falkor._update_config",
                side_effect=RuntimeError("config store unavailable"),
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is False
        assert "config store unavailable" in result["error"]
        assert not (tmp_path / "bootstrap.yaml").exists()

    def test_install_uses_bootstrap_database_path_when_bootstrap_exists(
        self, tmp_path: Path
    ) -> None:
        module = _falkor_module()
        configured_db = tmp_path / "custom-hub.db"
        db = _new_config_db(configured_db)
        db.close()
        bootstrap_path = tmp_path / "bootstrap.yaml"
        bootstrap_path.write_text(f"database_path: {configured_db}\n", encoding="utf-8")
        bootstrap_path.chmod(0o600)

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert (
            ConfigStore(LocalDatabase(configured_db)).get("databases.falkordb.host") == "localhost"
        )
        assert not (tmp_path / "gobby-hub.db").exists()

    def test_install_none_home_normalizes_before_helpers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _falkor_module()
        received_homes: list[Path | None] = []

        monkeypatch.setattr(module, "get_gobby_home", lambda: tmp_path)

        original_resolve_password = module._resolve_falkordb_password
        original_resolve_db_path = module._resolve_falkordb_db_path
        original_update_config = module._update_config

        def track_password(password: str | None = None, *, gobby_home: Path | None = None) -> str:
            received_homes.append(gobby_home)
            return original_resolve_password(password, gobby_home=gobby_home)

        def track_db_path(home: Path) -> Path:
            received_homes.append(home)
            return original_resolve_db_path(home)

        def track_update_config(
            *args: object, gobby_home: Path | None = None, **kwargs: object
        ) -> None:
            received_homes.append(gobby_home)
            original_update_config(*args, gobby_home=gobby_home, **kwargs)

        monkeypatch.setattr(module, "_resolve_falkordb_password", track_password)
        monkeypatch.setattr(module, "_resolve_falkordb_db_path", track_db_path)
        monkeypatch.setattr(module, "_update_config", track_update_config)

        db = _new_config_db(tmp_path / "gobby-hub.db")
        db.close()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkor._wait_for_health", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            module.install_falkordb(gobby_home=None, password="secret")

        assert received_homes
        assert all(home == tmp_path for home in received_homes)


class TestUninstallFalkorDB:
    def test_uninstall_none_home_normalizes_before_cleanup_helpers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _falkor_module()
        received_homes: list[Path | None] = []
        services_dir = tmp_path / "services"
        services_dir.mkdir(parents=True)
        (services_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

        monkeypatch.setattr(module, "get_gobby_home", lambda: tmp_path)

        original_update_config = module._update_config

        def track_update_config(
            *args: object, gobby_home: Path | None = None, **kwargs: object
        ) -> None:
            received_homes.append(gobby_home)
            original_update_config(*args, gobby_home=gobby_home, **kwargs)

        monkeypatch.setattr(module, "_update_config", track_update_config)

        with patch("gobby.cli.installers.falkor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            result = module.uninstall_falkordb(gobby_home=None)

        assert result["success"] is True
        assert received_homes == [tmp_path]
