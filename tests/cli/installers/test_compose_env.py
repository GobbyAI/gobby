"""Canonical environment resolution for managed-service Compose commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.cli.installers import compose_env

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


class _ConfigStore:
    values: dict[str, Any] = {}

    def __init__(self, _db: object) -> None:
        pass

    def list_keys(self) -> list[str]:
        return list(self.values)

    def get(self, key: str) -> Any:
        return self.values.get(key)


class _SecretStore:
    values: dict[str, str] = {}

    def __init__(self, _db: object) -> None:
        pass

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
    _ConfigStore.values = {
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
    monkeypatch.setattr("gobby.storage.config_store.ConfigStore", _ConfigStore)
    monkeypatch.setattr("gobby.storage.secrets.SecretStore", _SecretStore)

    env = compose_env._service_environment(tmp_path)

    assert env == {
        "GOBBY_QDRANT_HTTP_PORT": "7333",
        "GOBBY_QDRANT_GRPC_PORT": "7334",
        "GOBBY_FALKORDB_PASSWORD": "falkor-secret",
        "GOBBY_FALKORDB_PORT": "17000",
    }
    assert db.closed is True


def test_missing_falkordb_secret_is_actionable_without_generating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ConfigStore.values = {
        "databases.falkordb.host": "127.0.0.1",
        "databases.falkordb.port": 16379,
        "databases.falkordb.password": "$secret:missing",
    }
    _SecretStore.values = {}
    monkeypatch.setattr(
        "gobby.storage.hub.runtime.runtime_hub_database",
        lambda *_args, **_kwargs: _Db(),
    )
    monkeypatch.setattr("gobby.storage.config_store.ConfigStore", _ConfigStore)
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

    with pytest.raises(compose_env.ComposeEnvironmentError, match="valid TCP port"):
        compose_env.resolve_compose_runtime(tmp_path, profiles=("postgres",))
