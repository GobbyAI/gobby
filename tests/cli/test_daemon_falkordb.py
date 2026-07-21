"""Daemon service-start tests for FalkorDB wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.daemon import _services_start
from gobby.config.app import load_config
from gobby.storage.config_store import ConfigStore
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def _seed_falkordb_config(db: HubDatabase, password: str) -> None:
    config_store = ConfigStore(db)
    secret_store = SecretStore(db)
    config_store.set("databases.qdrant.url", "http://localhost:6333", source="test")
    config_store.set("databases.qdrant.port", 6333, source="test")
    config_store.set("databases.falkordb.host", "127.0.0.1", source="test")
    config_store.set("databases.falkordb.port", 16379, source="test")
    config_store.set_secret(
        "databases.falkordb.password",
        password,
        secret_store,
        source="test",
    )


def test_load_config_resolves_falkordb_secret_with_secret_store_get(
    tmp_path, monkeypatch: pytest.MonkeyPatch, postgres_db: HubDatabase
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    config_store = ConfigStore(postgres_db)
    secret_store = SecretStore(postgres_db)
    config_store.set_secret(
        "databases.falkordb.password",
        "plain-secret",
        secret_store,
        source="test",
    )

    config = load_config(config_store=config_store, secret_resolver=secret_store.get)

    assert config.databases.falkordb.password == "plain-secret"


def test_services_start_uses_falkordb_config_store_password(
    tmp_path, monkeypatch: pytest.MonkeyPatch, postgres_db: HubDatabase
) -> None:
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
        patch("gobby.storage.hub.runtime.open_runtime_hub_database", return_value=postgres_db),
        patch("gobby.cli.daemon.subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0)
        _services_start(tmp_path)

    cmd = mock_run.call_args.args[0]
    assert "--profile" in cmd
    assert "falkordb" in cmd
    assert "neo4j" not in cmd
    assert mock_run.call_args.kwargs["env"]["GOBBY_FALKORDB_PASSWORD"] == "config-secret"
    assert "GOBBY_NEO4J_PASSWORD" not in mock_run.call_args.kwargs["env"]
