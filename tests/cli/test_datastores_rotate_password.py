"""`gobby datastores rotate-password` contracts."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import click
import psycopg
import pytest
from click.testing import CliRunner

import gobby.cli.datastores as datastores
import gobby.cli.installers.falkor as falkor
from gobby.cli import cli
from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.bootstrap_io import read_bootstrap_yaml, write_bootstrap_yaml
from gobby.config.persistence import validate_falkordb_password
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore

pytestmark = pytest.mark.unit

_CURRENT_DSN = "postgresql://gobby:old-secret@localhost:60891/gobby"
_URL_SAFE = re.compile(r"[A-Za-z0-9_-]+")


def _write_bootstrap(home: Path, *, datastore_mode: str = "local") -> None:
    files_home = home / "files"
    files_home.mkdir(exist_ok=True)
    data: dict[str, Any] = {"datastore_mode": datastore_mode}
    if datastore_mode == "local":
        data.update({"files_home": str(files_home), "database_url": _CURRENT_DSN})
    else:
        data["hub_daemon_url"] = "http://hub.example:60887"
    write_bootstrap_yaml(home / "bootstrap.yaml", data)


class _FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: Any) -> None:
        self.statements.append(query.as_string(None))


class _NonClosingDb:
    def __init__(self, db: HubDatabase) -> None:
        self._db = db

    def __getattr__(self, name: str) -> object:
        return getattr(self._db, name)

    def close(self) -> None:
        pass


@contextmanager
def _patch_config_db(monkeypatch: pytest.MonkeyPatch, db: HubDatabase) -> Iterator[None]:
    @contextmanager
    def open_db(_home: object = None, *, apply_migrations: bool = True) -> Iterator[HubDatabase]:
        _ = apply_migrations
        yield cast(HubDatabase, _NonClosingDb(db))

    monkeypatch.setattr(falkor, "_config_db", open_db)
    yield


@pytest.fixture
def rotation_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A local bootstrap under ``tmp_path`` with every process spawn refused."""

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("rotate-password must never spawn a process")

    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)
    monkeypatch.setattr(datastores, "get_gobby_home", lambda: tmp_path)
    _write_bootstrap(tmp_path)
    return tmp_path


def _patch_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeConnection, list[tuple[str, dict[str, Any]]]]:
    connection = _FakeConnection()
    connects: list[tuple[str, dict[str, Any]]] = []

    def _connect(dsn: str, **kwargs: Any) -> _FakeConnection:
        connects.append((dsn, kwargs))
        return connection

    monkeypatch.setattr(psycopg, "connect", _connect)
    return connection, connects


def _new_password(home: Path) -> tuple[str, str]:
    new_url = read_bootstrap_yaml(home / "bootstrap.yaml")["database_url"]
    parts = urlsplit(new_url)
    assert (parts.username, parts.hostname, parts.port, parts.path) == (
        "gobby",
        "localhost",
        60891,
        "/gobby",
    )
    assert parts.password is not None
    return new_url, unquote(parts.password)


def test_postgres_rotation_alters_role_then_rewrites_bootstrap(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    connection, connects = _patch_connect(monkeypatch)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 0, result.output
    assert connects == [(_CURRENT_DSN, {"connect_timeout": 5, "autocommit": True})]
    _new_url, password = _new_password(rotation_home)
    assert password != "old-secret"
    assert len(password) >= 32
    assert _URL_SAFE.fullmatch(password)
    assert connection.statements == [f"ALTER ROLE \"gobby\" PASSWORD '{password}'"]
    assert password not in result.output
    assert result.output.strip() == "Run `gobby restart` to apply the new postgres password."


def test_postgres_rotation_keeps_bootstrap_when_alter_role_fails(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    def _connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", _connect)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert "PostgreSQL password rotation failed: connection refused" in result.output
    assert read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["database_url"] == _CURRENT_DSN


def test_postgres_rotation_reports_new_dsn_when_bootstrap_write_fails(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    connection, _connects = _patch_connect(monkeypatch)
    captured: dict[str, str] = {}

    def _fail_write(*, gobby_home: Path, database_url: str) -> None:
        _ = gobby_home
        captured["database_url"] = database_url
        raise BootstrapConfigError("disk full")

    monkeypatch.setattr(datastores, "write_postgres_defaults", _fail_write)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert len(connection.statements) == 1
    new_url = captured["database_url"]
    password = unquote(cast(str, urlsplit(new_url).password))
    assert connection.statements[0].endswith(f"'{password}'")
    assert f"Set database_url in {rotation_home / 'bootstrap.yaml'} to: {new_url}" in result.output
    assert "bootstrap.yaml update failed after the role changed" in result.output
    assert read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["database_url"] == _CURRENT_DSN


def test_falkordb_rotation_stores_a_new_secret_without_docker(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path, hub_db: HubDatabase
) -> None:
    with _patch_config_db(monkeypatch, hub_db):
        falkor._update_config(port=16379, password="old-falkor", gobby_home=rotation_home)

        result = CliRunner().invoke(cli, ["datastores", "rotate-password", "falkordb"])

        assert result.exit_code == 0, result.output
        overrides = ConfigRepository(hub_db).read(resolve_secrets=False).overrides
        stored = SecretStore(hub_db, gobby_home=rotation_home).get("falkordb_password")

    assert overrides["databases.falkordb.password"] == "$secret:falkordb_password"
    assert stored is not None
    assert stored != "old-falkor"
    assert len(stored) == 32
    assert validate_falkordb_password(stored) == stored
    assert stored not in result.output
    assert result.output.strip() == "Run `gobby restart` to apply the new falkordb password."
    assert read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["database_url"] == _CURRENT_DSN


def test_falkordb_rotation_reports_unreachable_hub(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    @contextmanager
    def _offline(_home: object = None, *, apply_migrations: bool = True) -> Iterator[None]:
        _ = apply_migrations
        raise BootstrapConfigError("hub offline")
        yield

    monkeypatch.setattr(falkor, "_config_db", _offline)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "falkordb"])

    assert result.exit_code == 1
    assert "FalkorDB password rotation failed: hub offline" in result.output


@pytest.mark.parametrize("service", ["postgres", "falkordb"])
def test_remote_mode_is_refused_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, service: str
) -> None:
    monkeypatch.setattr(datastores, "get_gobby_home", lambda: tmp_path)
    _write_bootstrap(tmp_path, datastore_mode="remote")

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", service])

    assert result.exit_code == 2
    assert "remote clients hold no datastore credentials" in result.output


def test_missing_bootstrap_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(datastores, "get_gobby_home", lambda: tmp_path)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert "bootstrap.yaml is missing; run `gobby install`" in result.output


def test_unknown_service_is_rejected() -> None:
    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "qdrant"])

    assert result.exit_code == 2
    assert "is not one of" in result.output


@pytest.mark.parametrize(
    ("database_url", "expected_role", "expected_url"),
    [
        (
            "postgresql://gobby:old@localhost:60891/gobby",
            "gobby",
            "postgresql://gobby:new%2Fpw@localhost:60891/gobby",
        ),
        (
            "postgresql://ro%40le:old@[::1]:5432/db?sslmode=require",
            "ro@le",
            "postgresql://ro%40le:new%2Fpw@[::1]:5432/db?sslmode=require",
        ),
        (
            "postgresql://gobby@localhost/gobby",
            "gobby",
            "postgresql://gobby:new%2Fpw@localhost/gobby",
        ),
    ],
)
def test_dsn_with_password_replaces_only_the_password(
    database_url: str, expected_role: str, expected_url: str
) -> None:
    assert datastores._dsn_with_password(database_url, "new/pw") == (expected_role, expected_url)


def test_dsn_with_password_requires_a_user() -> None:
    with pytest.raises(click.ClickException, match="names no user"):
        datastores._dsn_with_password("postgresql://localhost/gobby", "new")
