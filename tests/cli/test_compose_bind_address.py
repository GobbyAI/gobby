"""Managed-datastore bind address and published-host contracts."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from gobby.cli.installers import compose_env, falkor, qdrant
from gobby.config.persistence import DatabasesConfig
from gobby.storage.config_mutations import ConfigPatch

pytestmark = pytest.mark.unit


class _Database:
    def transaction(self) -> Any:
        return nullcontext()


class _ConfigStore:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def read_snapshot(self) -> SimpleNamespace:
        return SimpleNamespace(revision=0, values=self.values, overrides=self.values)

    def patch(self, *, expected_revision: int, patch: ConfigPatch) -> None:
        assert expected_revision == 0
        self.values.update(patch.values)
        self.values.update({key: f"$secret:{key.replace('.', '_')}" for key in patch.secrets})
        for key in patch.unset:
            self.values.pop(key, None)


def test_templates_parameterized_and_identical() -> None:
    python_template = Path("src/gobby/data/docker-compose.services.yml")
    rust_template = Path("crates/gcore/assets/docker-compose.services.yml")

    assert not rust_template.exists()
    loaded = yaml.safe_load(python_template.read_text(encoding="utf-8"))
    services = loaded["services"]
    assert "gobby_files" not in loaded.get("volumes", {})
    lifecycle = loaded["x-gobby-lifecycle"]["gobby_files"]
    assert lifecycle["type"] == "bind"
    assert "GOBBY_FILES_HOME" in lifecycle["source"]
    bind = "${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}"
    assert services["postgres"]["ports"] == [f"{bind}:${{GOBBY_POSTGRES_PORT:-60891}}:5432"]
    assert services["qdrant"]["ports"] == [
        f"{bind}:${{GOBBY_QDRANT_HTTP_PORT:-6333}}:6333",
        f"{bind}:${{GOBBY_QDRANT_GRPC_PORT:-6334}}:6334",
    ]
    assert services["falkordb"]["ports"] == [
        f"{bind}:${{GOBBY_FALKORDB_PORT:-16379}}:6379",
        "127.0.0.1:${GOBBY_FALKORDB_BROWSER_PORT:-13000}:3000",
    ]


def test_resolve_compose_runtime_reads_bind_from_bootstrap_without_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    files_home = tmp_path / "files-home"
    files_home.mkdir()
    bootstrap = tmp_path / "bootstrap.yaml"
    bootstrap.write_text(
        yaml.safe_dump(
            {
                "datastore_mode": "local",
                "database_url": "postgresql://gobby:secret@localhost:60891/gobby",
                "services_bind_address": "100.64.0.7",
                "files_home": str(files_home),
            }
        ),
        encoding="utf-8",
    )
    bootstrap.chmod(0o600)
    monkeypatch.setattr(
        compose_env,
        "_pgsearch_environment",
        lambda: {"GOBBY_PG_SEARCH_VERSION": "1", "GOBBY_PG_SEARCH_SHA256": "hash"},
    )

    runtime = compose_env.resolve_compose_runtime(tmp_path, profiles=("postgres",))

    assert runtime.environment["GOBBY_SERVICES_BIND_ADDRESS"] == "100.64.0.7"
    assert runtime.environment["GOBBY_FILES_HOME"] == str(files_home)


def test_installers_respect_published_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    values: dict[str, Any] = {"databases.published_host": "hub.tailnet.ts.net"}
    store = _ConfigStore(values)
    database = _Database()
    monkeypatch.setattr(falkor, "_config_db", lambda *_args, **_kwargs: nullcontext(database))
    monkeypatch.setattr(
        "gobby.storage.hub.runtime.runtime_hub_database",
        lambda *_args, **_kwargs: nullcontext(database),
    )
    monkeypatch.setattr(
        "gobby.storage.config_store.ConfigStore",
        lambda _db, **_kwargs: store,
    )
    monkeypatch.setattr("gobby.storage.secrets.SecretStore", lambda *_args, **_kwargs: object())

    falkor._update_config(port=16379, password="secret", gobby_home=tmp_path)
    qdrant._update_config(qdrant_port=6333, gobby_home=tmp_path)

    assert values["databases.falkordb.host"] == "hub.tailnet.ts.net"
    assert values["databases.qdrant.url"] == "http://hub.tailnet.ts.net:6333"


def test_databases_config_exposes_published_host() -> None:
    config = DatabasesConfig.model_validate({"published_host": "hub.tailnet.ts.net"})

    assert config.published_host == "hub.tailnet.ts.net"
