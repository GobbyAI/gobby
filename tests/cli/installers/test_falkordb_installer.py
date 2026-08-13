"""Tests for the FalkorDB installer contract."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import yaml

from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def _falkor_module() -> Any:
    return importlib.import_module("gobby.cli.installers.falkor")


def _read_compose_services(repo_root: Path) -> dict[str, Any]:
    compose_path = repo_root / "src/gobby/data/docker-compose.services.yml"
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


class _NonClosingDb:
    dialect = "postgres"

    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def __getattr__(self, name: str) -> object:
        return getattr(self._db, name)

    def close(self) -> None:
        pass


@contextmanager
def _patch_config_db(db: HubDatabase) -> Iterator[None]:
    @contextmanager
    def open_db(_home: object = None, *, apply_migrations: bool = True) -> Iterator[HubDatabase]:
        _ = apply_migrations
        yield cast(HubDatabase, _NonClosingDb(db))

    with (
        patch("gobby.cli.installers.falkor._config_db", side_effect=open_db),
        patch("gobby.storage.hub.runtime.runtime_hub_database", side_effect=open_db),
        patch(
            "gobby.cli.installers.compose_env._bootstrap_database_url",
            return_value="postgresql://gobby:postgres-secret@localhost:5432/gobby",
        ),
    ):
        yield


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
        assert (
            "${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:${GOBBY_FALKORDB_PORT:-16379}:6379"
            in falkordb["ports"]
        )
        assert "127.0.0.1:${GOBBY_FALKORDB_BROWSER_PORT:-13000}:3000" in falkordb["ports"]
        assert (
            "REDIS_ARGS=--requirepass ${GOBBY_FALKORDB_PASSWORD:-gobbyfalkor}"
            in falkordb["environment"]
        )
        assert "healthcheck" in falkordb
        assert "redis-cli" in " ".join(falkordb["healthcheck"]["test"])


class TestInstallFalkorDB:
    def test_installer_runs_falkordb_compose_profile_and_healthcheck(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            _patch_config_db(hub_db),
        ):
            mock_run.side_effect = _docker_run_side_effect

            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is True
        assert result["password_source"] == "provided"
        assert result["password"] is None
        assert result["browser_url"] == "http://localhost:13000"
        assert result["url"] == "redis://127.0.0.1:16379"
        assert result["mode"] == "docker"
        assert "error" not in result
        assert "compose_running" not in result
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
            "sh",
            "-c",
            'redis-cli -a "$GOBBY_FALKORDB_PASSWORD" PING',
        ]
        assert mock_run.call_args_list[0].kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "secret"
        assert mock_run.call_args_list[0].kwargs["cwd"] == str(tmp_path / "services")
        assert mock_run.call_args_list[1].kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "secret"
        assert mock_run.call_args_list[1].kwargs["cwd"] == str(tmp_path / "services")

    def test_generated_password_result_discloses_generated_value(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            patch(
                "gobby.cli.installers.falkor._generate_falkordb_password", return_value="generated"
            ),
            _patch_config_db(hub_db),
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password=None)

        assert result["success"] is True
        assert result["password_source"] == "generated"
        assert result["password"] == "generated"
        assert result["browser_url"] == "http://localhost:13000"
        assert "error" not in result
        assert "compose_running" not in result

    def test_provided_password_result_does_not_disclose_password(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            _patch_config_db(hub_db),
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password="provided")

        assert result["success"] is True
        assert result["password_source"] == "provided"
        assert result["password"] is None

    def test_reused_password_result_does_not_disclose_password(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()
        secret_store = SecretStore(hub_db, gobby_home=tmp_path)
        ConfigMutations(hub_db, secret_store=secret_store).patch_internal(
            expected_revision=0,
            patch=ConfigPatch(
                values={
                    "databases.falkordb.host": "127.0.0.1",
                    "databases.falkordb.port": 16379,
                },
                secrets={"databases.falkordb.password": SecretUpdate("reused")},
            ),
            source="test",
        )

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            _patch_config_db(hub_db),
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password=None)

        assert result["success"] is True
        assert result["password_source"] == "reused"
        assert result["password"] is None
        assert mock_run.call_args_list[0].kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "reused"

    def test_generated_passwords_validate(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()
        with _patch_config_db(hub_db):
            for _ in range(100):
                resolved = module._resolve_falkordb_password(None, gobby_home=tmp_path)
                assert resolved.source == "generated"
                assert module.validate_falkordb_password(resolved.value) == resolved.value

    def test_installer_persists_password_only_to_config_store(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            _patch_config_db(hub_db),
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is True

        assert not (tmp_path / "bootstrap.yaml").exists()

        snapshot = ConfigRepository(hub_db).read(resolve_secrets=False)
        assert snapshot.values["databases.falkordb.host"] == "127.0.0.1"
        assert snapshot.values["databases.falkordb.port"] == 16379
        assert snapshot.overrides["databases.falkordb.password"] == "$secret:falkordb_password"

        row = hub_db.fetchone(
            "SELECT value, is_secret FROM config_store WHERE key = %s",
            ("databases.falkordb.password",),
        )
        assert row is not None
        assert json.loads(row["value"]) == "$secret:falkordb_password"
        assert row["is_secret"] is True
        assert hub_db.fetchone("SELECT 1 FROM secrets WHERE name = %s", ("falkordb_password",))
        assert SecretStore(hub_db, gobby_home=tmp_path).get("falkordb_password") == "secret"

    def test_installer_stops_before_compose_when_config_store_update_fails(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch("gobby.cli.installers.falkor.subprocess.run") as mock_run,
            _patch_config_db(hub_db),
            patch(
                "gobby.cli.installers.falkor._update_config",
                side_effect=RuntimeError("disk full"),
            ),
        ):
            mock_run.side_effect = _docker_run_side_effect
            result = module.install_falkordb(gobby_home=tmp_path, password="secret")

        assert result["success"] is False
        assert "FalkorDB config" in result["error"]
        mock_run.assert_not_called()
        assert not (tmp_path / "bootstrap.yaml").exists()

    def test_incomplete_canonical_config_does_not_generate_replacement_password(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()
        mutations = ConfigMutations(hub_db)
        mutations.patch(
            expected_revision=mutations.repository.current_revision(),
            patch=ConfigPatch(values={"databases.falkordb.host": "falkor.internal"}),
        )

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            _patch_config_db(hub_db),
            patch("gobby.cli.installers.falkor._generate_falkordb_password") as generate_password,
        ):
            result = module.install_falkordb(gobby_home=tmp_path)

        assert result["success"] is False
        assert "incomplete" in result["error"]
        generate_password.assert_not_called()

    def test_config_db_opener_uses_home_bootstrap_path(self, tmp_path: Path) -> None:
        module = _falkor_module()

        with patch(
            "gobby.storage.hub.runtime.runtime_hub_database",
            return_value=object(),
        ) as open_db:
            assert module._config_db(tmp_path, apply_migrations=False) is open_db.return_value

        open_db.assert_called_once_with(
            str(tmp_path / "bootstrap.yaml"),
            apply_migrations=False,
        )

    def test_install_none_home_normalizes_before_helpers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()
        received_homes: list[Path | None] = []

        monkeypatch.setattr("gobby.cli.utils.get_gobby_home", lambda: tmp_path)

        original_resolve_password = module._resolve_falkordb_password
        original_update_config = module._update_config

        def track_password(password: str | None = None, *, gobby_home: Path | None = None) -> Any:
            received_homes.append(gobby_home)
            return original_resolve_password(password, gobby_home=gobby_home)

        @contextmanager
        def track_config_db(home: Path, *, apply_migrations: bool = True) -> Iterator[HubDatabase]:
            _ = apply_migrations
            received_homes.append(home)
            yield cast(HubDatabase, _NonClosingDb(hub_db))

        def track_update_config(
            *args: object, gobby_home: Path | None = None, **kwargs: object
        ) -> None:
            received_homes.append(gobby_home)
            original_update_config(*args, gobby_home=gobby_home, **kwargs)

        monkeypatch.setattr(module, "_resolve_falkordb_password", track_password)
        monkeypatch.setattr(module, "_config_db", track_config_db)
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
    def test_uninstall_clears_config_store(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()

        with _patch_config_db(hub_db):
            module._update_config(
                port=16379,
                password="secret",
                gobby_home=tmp_path,
            )

            result = module.uninstall_falkordb(gobby_home=tmp_path)

            secret_store = SecretStore(hub_db, gobby_home=tmp_path)
            overrides = ConfigRepository(hub_db).read(resolve_secrets=False).overrides
            assert result["success"] is True
            assert "databases.falkordb.host" not in overrides
            assert "databases.falkordb.port" not in overrides
            assert "databases.falkordb.password" not in overrides
            assert secret_store.get("falkordb_password") is None

        assert not (tmp_path / "bootstrap.yaml").exists()

    def test_uninstall_runs_falkordb_compose_down(
        self,
        tmp_path: Path,
        hub_db: HubDatabase,
    ) -> None:
        module = _falkor_module()

        with (
            patch.object(shutil, "which", return_value="/usr/bin/docker"),
            patch(
                "gobby.cli.installers.falkor.subprocess.run", return_value=_successful_run()
            ) as mock_run,
            _patch_config_db(hub_db),
        ):
            module._update_config(
                port=16379,
                password="secret",
                gobby_home=tmp_path,
            )
            from gobby.cli.installers.postgres import reconcile_unified_compose

            compose_file = reconcile_unified_compose(tmp_path / "services").compose_file
            result = module.uninstall_falkordb(gobby_home=tmp_path)

        assert result["success"] is True
        assert result["compose_stopped"] is True
        mock_run.assert_called_once_with(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "--profile",
                "falkordb",
                "down",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=mock_run.call_args.kwargs["env"],
            cwd=str(tmp_path / "services"),
        )
        assert mock_run.call_args.kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "secret"
