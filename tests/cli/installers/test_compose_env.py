"""Canonical environment resolution for managed-service Compose commands."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.cli.installers import compose_env
from gobby.storage.config_mutations import ConfigMutations, ConfigPatch, SecretUpdate
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit


class _Db:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _Db:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class _ConfigRepository:
    values: dict[str, Any] = {}

    def __init__(self, _db: object, **_kwargs: object) -> None:
        pass

    def read(self, *, resolve_secrets: bool = True) -> SimpleNamespace:
        del resolve_secrets
        return SimpleNamespace(values=self.values)


class _SecretStore:
    values: dict[str, str] = {}
    gobby_homes: list[Path | None] = []

    def __init__(self, _db: object, *, gobby_home: Path | None = None) -> None:
        self.gobby_homes.append(gobby_home)

    def exists(self, name: str) -> bool:
        return name in self.values

    def get(self, name: str) -> str | None:
        return self.values.get(name)


def test_postgres_environment_parses_encoded_bootstrap_dsn(tmp_path: Path) -> None:
    env = compose_env._postgres_environment(
        tmp_path,
        database_url="postgresql://gobby:encoded%2Fsecret@localhost:6543/gobby_hub",
    )

    assert env == {
        "GOBBY_POSTGRES_DB": "gobby_hub",
        "GOBBY_POSTGRES_USER": "gobby",
        "GOBBY_POSTGRES_PASSWORD": "encoded/secret",
        "GOBBY_POSTGRES_PORT": "6543",
    }


def test_runtime_process_and_explicit_values_override_canonical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        compose_env,
        "_postgres_environment",
        lambda *_args, **_kwargs: {
            "GOBBY_POSTGRES_DB": "gobby",
            "GOBBY_POSTGRES_USER": "gobby",
            "GOBBY_POSTGRES_PASSWORD": "canonical",
            "GOBBY_POSTGRES_PORT": "5432",
        },
    )
    monkeypatch.setattr(
        compose_env,
        "_pgsearch_environment",
        lambda: {
            "GOBBY_PG_SEARCH_VERSION": "canonical-version",
            "GOBBY_PG_SEARCH_SHA256": "canonical-hash",
        },
    )
    monkeypatch.setattr(
        compose_env,
        "_service_environment",
        lambda _home, **_kwargs: {
            "GOBBY_QDRANT_HTTP_PORT": "6333",
            "GOBBY_QDRANT_GRPC_PORT": "6334",
        },
    )
    monkeypatch.setenv("GOBBY_POSTGRES_PASSWORD", "process")
    files_home = tmp_path / "files-home"
    files_home.mkdir()
    from gobby.config.bootstrap_io import write_bootstrap_yaml

    write_bootstrap_yaml(
        tmp_path / "bootstrap.yaml",
        {
            "datastore_mode": "local",
            "files_home": str(files_home),
            "daemon_port": 60887,
            "bind_host": "127.0.0.1",
        },
    )

    runtime = compose_env.resolve_compose_runtime(
        tmp_path,
        profiles=("qdrant",),
        overrides={"GOBBY_POSTGRES_PASSWORD": "explicit"},
    )

    assert runtime.environment["GOBBY_POSTGRES_PASSWORD"] == "explicit"
    assert runtime.environment["GOBBY_QDRANT_HTTP_PORT"] == "6333"
    assert runtime.profiles == ("qdrant",)


def test_service_environment_restores_persisted_custom_qdrant_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = _Db()
    _SecretStore.gobby_homes = []
    _ConfigRepository.values = {
        "databases.qdrant.url": "http://localhost:7333",
        "databases.qdrant.port": 7333,
        "databases.falkordb.host": "127.0.0.1",
        "databases.falkordb.port": 17000,
        "databases.falkordb.password": "$secret:falkor-test",
    }
    _SecretStore.values = {"falkor-test": "falkor-secret"}
    monkeypatch.setattr(
        "gobby.storage.hub.runtime.runtime_hub_database",
        lambda *_args, **_kwargs: db,
    )
    monkeypatch.setattr("gobby.storage.config_repository.ConfigRepository", _ConfigRepository)
    monkeypatch.setattr("gobby.storage.secrets.SecretStore", _SecretStore)

    env = compose_env._service_environment(tmp_path)

    assert env == {
        "GOBBY_QDRANT_HTTP_PORT": "7333",
        "GOBBY_QDRANT_GRPC_PORT": "7334",
        "GOBBY_FALKORDB_HOST": "127.0.0.1",
        "GOBBY_FALKORDB_PASSWORD": "falkor-secret",
        "GOBBY_FALKORDB_PORT": "17000",
    }
    assert _SecretStore.gobby_homes == [tmp_path]
    assert db.closed is True


def test_service_environment_decrypts_falkor_secret_from_explicit_home(
    hub_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ambient_home = tmp_path / "ambient"
    explicit_home = tmp_path / "managed"
    monkeypatch.setenv("GOBBY_HOME", str(ambient_home))

    secret_store = SecretStore(hub_db, gobby_home=explicit_home)
    ConfigMutations(hub_db, secret_store=secret_store).patch(
        expected_revision=0,
        patch=ConfigPatch(
            values={
                "databases.falkordb.host": "127.0.0.1",
                "databases.falkordb.port": 16379,
            },
            secrets={"databases.falkordb.password": SecretUpdate("managed-secret")},
        ),
    )

    opened_bootstrap_paths: list[str | Path] = []

    def open_db(bootstrap_path: str | Path, **_kwargs: object) -> object:
        opened_bootstrap_paths.append(bootstrap_path)
        return nullcontext(hub_db)

    monkeypatch.setattr("gobby.storage.hub.runtime.runtime_hub_database", open_db)

    env = compose_env._service_environment(
        explicit_home,
        required_profiles=("falkordb",),
    )

    assert opened_bootstrap_paths == [str(explicit_home / "bootstrap.yaml")]
    assert env["GOBBY_FALKORDB_HOST"] == "127.0.0.1"
    assert env["GOBBY_FALKORDB_PASSWORD"] == "managed-secret"
    assert (explicit_home / ".secret_kek").exists()
    assert not (ambient_home / ".secret_kek").exists()


def test_invalid_falkordb_host_is_actionable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ConfigRepository.values = {
        "databases.falkordb.host": "",
        "databases.falkordb.port": 16379,
        "databases.falkordb.password": "$secret:falkor-test",
    }
    _SecretStore.values = {"falkor-test": "falkor-secret"}
    monkeypatch.setattr(
        "gobby.storage.hub.runtime.runtime_hub_database",
        lambda *_args, **_kwargs: _Db(),
    )
    monkeypatch.setattr("gobby.storage.config_repository.ConfigRepository", _ConfigRepository)
    monkeypatch.setattr("gobby.storage.secrets.SecretStore", _SecretStore)

    with pytest.raises(compose_env.ComposeEnvironmentError, match="host must be"):
        compose_env._service_environment(tmp_path, required_profiles=("falkordb",))


def test_missing_falkordb_secret_is_actionable_without_generating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ConfigRepository.values = {
        "databases.falkordb.host": "127.0.0.1",
        "databases.falkordb.port": 16379,
        "databases.falkordb.password": "$secret:missing",
    }
    _SecretStore.values = {}
    monkeypatch.setattr(
        "gobby.storage.hub.runtime.runtime_hub_database",
        lambda *_args, **_kwargs: _Db(),
    )
    monkeypatch.setattr("gobby.storage.config_repository.ConfigRepository", _ConfigRepository)
    monkeypatch.setattr("gobby.storage.secrets.SecretStore", _SecretStore)

    with pytest.raises(compose_env.ComposeEnvironmentError, match="missing"):
        compose_env._service_environment(tmp_path, required_profiles=("falkordb",))


def test_missing_bootstrap_reports_postgres_install_command(tmp_path: Path) -> None:
    with pytest.raises(compose_env.ComposeEnvironmentError, match="gobby postgres install"):
        compose_env._bootstrap_database_url(tmp_path)


def test_invalid_process_override_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        compose_env,
        "_postgres_environment",
        lambda *_args, **_kwargs: {
            "GOBBY_POSTGRES_DB": "gobby",
            "GOBBY_POSTGRES_USER": "gobby",
            "GOBBY_POSTGRES_PASSWORD": "secret",
            "GOBBY_POSTGRES_PORT": "5432",
        },
    )
    monkeypatch.setattr(
        compose_env,
        "_pgsearch_environment",
        lambda: {
            "GOBBY_PG_SEARCH_VERSION": "1.0.0",
            "GOBBY_PG_SEARCH_SHA256": "hash",
        },
    )
    monkeypatch.setenv("GOBBY_POSTGRES_PORT", "invalid")
    files_home = tmp_path / "files-home"
    files_home.mkdir()
    from gobby.config.bootstrap_io import write_bootstrap_yaml

    write_bootstrap_yaml(
        tmp_path / "bootstrap.yaml",
        {
            "datastore_mode": "local",
            "files_home": str(files_home),
            "daemon_port": 60887,
            "bind_host": "127.0.0.1",
        },
    )

    with pytest.raises(compose_env.ComposeEnvironmentError, match="valid TCP port"):
        compose_env.resolve_compose_runtime(tmp_path, profiles=("postgres",))


def test_predecessor_service_runtime_reads_only_service_fields_and_honors_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cutover resolves services straight from config_store, before the auth cutover.

    The predecessor auth rows are still in place at this point, so this path reads the
    four `databases.*` keys itself instead of going through `ConfigRepository`. Values
    the postgres runtime already carries stay authoritative.
    """
    database = MagicMock()
    database.fetchall.return_value = [
        {"key": "databases.falkordb.host", "value": '"127.0.0.1"'},
        {"key": "databases.falkordb.password", "value": '"$secret:falkordb_password"'},
        {"key": "databases.falkordb.port", "value": "60992"},
        {"key": "databases.qdrant.port", "value": "60990"},
    ]

    @contextmanager
    def open_database(
        _config_file: str | None = None,
        *,
        apply_migrations: bool = True,
    ) -> Iterator[MagicMock]:
        assert apply_migrations is False
        yield database

    monkeypatch.setattr("gobby.storage.hub.runtime.runtime_hub_database", open_database)

    runtime = compose_env.resolve_predecessor_service_runtime(
        tmp_path,
        compose_env.ComposeRuntime(
            environment={
                "PGOPTIONS": "-c gobby.maintenance_epoch=e1",
                "GOBBY_FALKORDB_PASSWORD": "scratch-override",
            },
            profiles=("postgres",),
        ),
    )

    assert runtime.profiles == ("postgres", "qdrant", "falkordb")
    assert runtime.environment["GOBBY_QDRANT_HTTP_PORT"] == "60990"
    assert runtime.environment["GOBBY_QDRANT_GRPC_PORT"] == "60991"
    assert runtime.environment["GOBBY_FALKORDB_PORT"] == "60992"
    assert runtime.environment["GOBBY_FALKORDB_PASSWORD"] == "scratch-override"
    assert len(database.fetchall.call_args.args) == 1
