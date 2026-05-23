"""Tests for the FalkorDB installer contract."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.secrets import SecretStore

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


def _successful_run(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _docker_run_side_effect(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    command = args[0]
    if isinstance(command, list) and "exec" in command:
        return _successful_run("PONG\n")
    return _successful_run()


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

    def test_falkordb_service_has_profile_ports_auth_and_browser(self, repo_root: Path) -> None:
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
        ):
            mock_run.side_effect = _docker_run_side_effect

            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result == {
            "success": True,
            "password_source": "provided",
            "password": None,
            "browser_url": "http://localhost:13000",
            "error": None,
            "compose_running": False,
        }
        assert (tmp_path / "services" / "docker-compose.yml").exists()

        commands = [call.args[0] for call in mock_run.call_args_list]
        assert commands[0] == [
            "docker",
            "compose",
            "-f",
            str(tmp_path / "services/docker-compose.yml"),
            "--profile",
            "falkordb",
            "up",
            "-d",
            "--remove-orphans",
        ]
        assert commands[1] == [
            "docker",
            "compose",
            "-f",
            str(tmp_path / "services/docker-compose.yml"),
            "exec",
            "-T",
            "falkordb",
            "redis-cli",
            "-a",
            "secret",
            "PING",
        ]
        assert mock_run.call_args_list[0].kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "secret"
        assert mock_run.call_args_list[0].kwargs["cwd"] == str(tmp_path / "services")
        assert mock_run.call_args_list[1].kwargs["cwd"] == str(tmp_path / "services")

    def test_generated_password_result_discloses_generated_value(self, tmp_path: Path) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkor.secrets.token_urlsafe", return_value="generated"),
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password=None)

        assert result["success"] is True
        assert result["password_source"] == "generated"
        assert result["password"] == "generated"
        assert result["browser_url"] == "http://localhost:13000"
        assert result["error"] is None
        assert result["compose_running"] is False

    def test_provided_password_result_does_not_disclose_password(self, tmp_path: Path) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password="provided")

        assert result["success"] is True
        assert result["password_source"] == "provided"
        assert result["password"] is None

    def test_reused_password_result_does_not_disclose_password(self, tmp_path: Path) -> None:
        module = _falkor_module()
        db = _new_config_db(tmp_path / "gobby-hub.db")
        try:
            store = ConfigStore(db)
            secret_store = SecretStore(db)
            store.set_secret(
                "databases.falkordb.requirepass",
                "reused",
                secret_store,
                source="test",
            )
        finally:
            db.close()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password=None)

        assert result["success"] is True
        assert result["password_source"] == "reused"
        assert result["password"] is None
        assert mock_run.call_args_list[0].kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "reused"

    def test_generated_passwords_validate(self, tmp_path: Path) -> None:
        module = _falkor_module()
        for _ in range(100):
            resolved = module._resolve_falkordb_password(None, gobby_home=tmp_path)
            assert resolved.source == "generated"
            assert module.validate_falkordb_password(resolved.value) == resolved.value

    def test_installer_writes_password_to_bootstrap_and_config_store(self, tmp_path: Path) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is True

        bootstrap = yaml.safe_load((tmp_path / "bootstrap.yaml").read_text(encoding="utf-8"))
        assert bootstrap["falkordb_password"] == "secret"
        assert "neo4j_password" not in bootstrap

        verify_db = LocalDatabase(tmp_path / "gobby-hub.db")
        try:
            store = ConfigStore(verify_db)
            assert store.get("databases.falkordb.host") == "127.0.0.1"
            assert store.get("databases.falkordb.port") == 16379
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
            patch(
                "gobby.cli.installers.falkor._update_config",
                side_effect=RuntimeError("disk full"),
            ),
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is False
        assert "config_store" in result["error"]
        assert "gobby uninstall --falkordb" in result["error"]
        assert not (tmp_path / "bootstrap.yaml").exists()

    def test_bootstrap_write_failure_reports_running_container_state(self, tmp_path: Path) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            patch("gobby.cli.installers.falkor._write_bootstrap_password", return_value=False),
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is False
        assert result["compose_running"] is True
        assert "bootstrap.yaml write failed" in result["error"]
        assert "gobby uninstall --falkordb" in result["error"]

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
        ):
            mock_run.side_effect = _docker_run_side_effect
            module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert ConfigStore(LocalDatabase(configured_db)).get("databases.falkordb.host") == (
            "127.0.0.1"
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

        def track_password(
            password: str | None = None, *, gobby_home: Path | None = None
        ) -> module.ResolvedFalkorPassword:
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

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
        ):
            mock_run.side_effect = _docker_run_side_effect
            module.install_falkordb(gobby_home=None, password="secret")

        assert received_homes
        assert all(home == tmp_path for home in received_homes)


class TestUninstallFalkorDB:
    def test_uninstall_stops_profile_and_preserves_behavior_keys(self, tmp_path: Path) -> None:
        module = _falkor_module()
        services_dir = tmp_path / "services"
        db_path = tmp_path / "gobby-hub.db"
        services_dir.mkdir(parents=True)
        (services_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
        (tmp_path / "bootstrap.yaml").write_text(
            f"falkordb_password: stale\ndatabase_path: {db_path}\ndaemon_port: 60887\n",
            encoding="utf-8",
        )
        (tmp_path / "bootstrap.yaml").chmod(0o600)

        db = _new_config_db(db_path)
        try:
            store = ConfigStore(db)
            secret_store = SecretStore(db)
            store.set("databases.falkordb.host", "127.0.0.1", source="test")
            store.set("databases.falkordb.port", 16379, source="test")
            store.set("databases.falkordb.graph_name", "custom", source="test")
            store.set_secret("databases.falkordb.requirepass", "secret", secret_store)
        finally:
            db.close()

        with patch("gobby.cli.installers.falkor.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run()
            result = module.uninstall_falkordb(gobby_home=tmp_path, purge=True)

        assert result == {"success": True, "data_removed": True}
        assert mock_run.call_args.args[0] == [
            "docker",
            "compose",
            "-f",
            str(services_dir / "docker-compose.yml"),
            "--profile",
            "falkordb",
            "down",
            "-v",
        ]
        assert mock_run.call_args.kwargs["cwd"] == str(services_dir)

        verify_db = LocalDatabase(db_path)
        try:
            store = ConfigStore(verify_db)
            assert store.get("databases.falkordb.host") is None
            assert store.get("databases.falkordb.port") is None
            assert store.get("databases.falkordb.requirepass") is None
            assert store.get("databases.falkordb.graph_name") == "custom"
        finally:
            verify_db.close()

        bootstrap = yaml.safe_load((tmp_path / "bootstrap.yaml").read_text(encoding="utf-8"))
        assert "falkordb_password" not in bootstrap
        assert bootstrap["daemon_port"] == 60887

    def test_uninstall_none_home_normalizes_before_cleanup_helpers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _falkor_module()
        received_homes: list[Path | None] = []
        services_dir = tmp_path / "services"
        services_dir.mkdir(parents=True)
        (services_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")

        monkeypatch.setattr(module, "get_gobby_home", lambda: tmp_path)

        original_clear_config = module._clear_config

        def track_clear_config(*args: object, gobby_home: Path | None = None) -> None:
            received_homes.append(gobby_home)
            original_clear_config(*args, gobby_home=gobby_home)

        monkeypatch.setattr(module, "_clear_config", track_clear_config)

        with patch("gobby.cli.installers.falkor.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run()
            result = module.uninstall_falkordb(gobby_home=None)

        assert result["success"] is True
        assert received_homes == [tmp_path]
