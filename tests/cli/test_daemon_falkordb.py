"""Daemon service-start tests for FalkorDB wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.daemon import _services_start
from gobby.config.app import load_config
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


def _seed_falkordb_config(home: Path, password: str) -> None:
    db = LocalDatabase(home / "gobby-hub.db")
    db.apply_migrations()
    try:
        config_store = ConfigStore(db)
        secret_store = SecretStore(db)
        config_store.set("databases.qdrant.url", None, source="test")
        config_store.set_secret(
            "databases.falkordb.requirepass",
            password,
            secret_store,
            source="test",
        )
    finally:
        db.close()


def test_load_config_resolves_falkordb_secret_with_secret_store_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    db = LocalDatabase(tmp_path / "gobby-hub.db")
    db.apply_migrations()
    try:
        config_store = ConfigStore(db)
        secret_store = SecretStore(db)
        config_store.set_secret(
            "databases.falkordb.requirepass",
            "plain-secret",
            secret_store,
            source="test",
        )

        config = load_config(config_store=config_store, secret_resolver=secret_store.get)
    finally:
        db.close()

    assert config.databases.falkordb.requirepass == "plain-secret"


def test_services_start_uses_falkordb_config_store_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services_dir = tmp_path / "services"
    services_dir.mkdir(parents=True)
    (services_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    _seed_falkordb_config(tmp_path, "config-secret")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("gobby.config.app.load_config", side_effect=load_config),
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
