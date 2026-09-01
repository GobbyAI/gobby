"""Daemon service-start tests for FalkorDB wiring."""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.daemon import _services_start
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def _seed_falkordb_config(db: HubDatabase, password: str) -> None:
    secret_store = SecretStore(db)
    ConfigMutations(db, secret_store=secret_store).patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "databases.qdrant.url": "http://localhost:6333",
                "databases.qdrant.port": 6333,
                "databases.falkordb.host": "127.0.0.1",
                "databases.falkordb.port": 16379,
            },
            secrets={"databases.falkordb.password": SecretUpdate(password)},
        ),
    )


def test_config_repository_resolves_falkordb_secret(
    tmp_path, monkeypatch: pytest.MonkeyPatch, postgres_db: HubDatabase
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    secret_store = SecretStore(postgres_db)
    ConfigMutations(postgres_db, secret_store=secret_store).patch(
        expected_revision=0,
        patch=ConfigPatch(secrets={"databases.falkordb.password": SecretUpdate("plain-secret")}),
    )

    snapshot = ConfigRepository(postgres_db, secret_store=secret_store).read()

    assert snapshot.secret_bindings["databases.falkordb.password"].plaintext == "plain-secret"


def test_services_start_uses_falkordb_config_store_password(
    tmp_path, monkeypatch: pytest.MonkeyPatch, postgres_db: HubDatabase
) -> None:
    files_home = tmp_path / "files"
    files_home.mkdir()
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text(
        f"datastore_mode: local\nfiles_home: {files_home}\n",
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    services_dir = tmp_path / "services"
    services_dir.mkdir(parents=True)
    (services_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  postgres:\n"
        "    profiles: [postgres]\n"
        "  qdrant:\n"
        "    profiles: [qdrant]\n"
        "  falkordb:\n"
        "    profiles: [falkordb]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    _seed_falkordb_config(postgres_db, "config-secret")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "gobby.cli.installers.compose_env._bootstrap_database_url",
            return_value="postgresql://gobby:postgres-secret@localhost:5432/gobby",
        ),
        patch(
            "gobby.storage.hub.runtime.runtime_hub_database",
            return_value=nullcontext(postgres_db),
        ),
        patch("gobby.cli.daemon.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        result = _services_start(tmp_path)

    assert result.outcome == "success", result.detail
    cmd = mock_run.call_args.args[0]
    assert "--profile" in cmd
    assert "falkordb" in cmd
    assert "neo4j" not in cmd
    assert mock_run.call_args.kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "config-secret"
    assert "GOBBY_NEO4J_PASSWORD" not in mock_run.call_args.kwargs["env"]
