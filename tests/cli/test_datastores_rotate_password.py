"""`gobby datastores rotate-password` contracts."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
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
from gobby.config.bootstrap_io import (
    read_bootstrap_yaml,
    update_bootstrap_yaml,
    write_bootstrap_yaml,
)
from gobby.config.persistence import validate_falkordb_password
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.async_ops import BoundedDBTimeoutError, IndeterminateCommitError
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


_FAKE_SCRAM = "SCRAM-SHA-256$4096:c2FsdA==$c3RvcmVka2V5:c2VydmVya2V5"


class _FakePGConn:
    """Minimal libpq handle exposing the SCRAM generator the ALTER path needs."""

    def __init__(self, verifier: bytes) -> None:
        self._verifier = verifier
        self.encrypted_passwords: list[str] = []
        self.finished = False

    def encrypt_password(self, password: bytes, user: bytes, algorithm: bytes) -> bytes:
        assert password and user
        assert algorithm == b"scram-sha-256"
        self.encrypted_passwords.append(password.decode("utf-8"))
        return self._verifier

    def finish(self) -> None:
        self.finished = True


class _FakeConnection:
    """Async connection double for ``run_bounded_db``'s dedicated connection."""

    def __init__(self, verifier: str = _FAKE_SCRAM, *, fail_close: bool = False) -> None:
        self.statements: list[str] = []
        self.committed = False
        self.closed = False
        self._fail_close = fail_close
        self.pgconn = _FakePGConn(verifier.encode("ascii"))

    async def execute(self, query: Any) -> None:
        self.statements.append(query.as_string(None))

    async def commit(self) -> None:
        self.committed = True

    async def close(self) -> None:
        if self._fail_close:
            raise RuntimeError("close failed after commit")
        self.closed = True


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
    *,
    fail_close: bool = False,
) -> tuple[_FakeConnection, list[tuple[str, dict[str, Any]]]]:
    connection = _FakeConnection(fail_close=fail_close)
    connects: list[tuple[str, dict[str, Any]]] = []

    async def _connect(dsn: str, **kwargs: Any) -> _FakeConnection:
        connects.append((dsn, kwargs))
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)
    return connection, connects


def _assert_single_bounded_connect(connects: list[tuple[str, dict[str, Any]]]) -> None:
    """The rotation dials the current DSN once on the bounded async helper."""
    assert len(connects) == 1
    dsn, kwargs = connects[0]
    assert dsn == _CURRENT_DSN
    assert kwargs["prepare_threshold"] is None
    assert kwargs["connect_timeout"] >= 1


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
    _assert_single_bounded_connect(connects)
    _new_url, password = _new_password(rotation_home)
    assert password != "old-secret"
    assert len(password) >= 32
    assert _URL_SAFE.fullmatch(password)
    # The statement carries the SCRAM verifier, never the plaintext password.
    assert len(connection.statements) == 1
    assert connection.statements[0] == f"ALTER ROLE \"gobby\" PASSWORD '{_FAKE_SCRAM}'"
    assert password not in connection.statements[0]
    assert connection.committed is True
    assert password not in result.output
    assert result.output.strip() == "Run `gobby restart` to apply the new postgres password."


def test_postgres_rotation_keeps_bootstrap_when_alter_role_fails(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    async def _connect(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert "PostgreSQL password rotation failed: OperationalError" in result.output
    assert read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["database_url"] == _CURRENT_DSN
    assert "credential_rotation" in read_bootstrap_yaml(rotation_home / "bootstrap.yaml")


def test_postgres_rotation_reports_new_dsn_when_bootstrap_write_fails(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    connection, _connects = _patch_connect(monkeypatch)
    captured: dict[str, str] = {}

    def _fail_write(
        *,
        gobby_home: Path,
        database_url: str,
        clear_credential_rotation: bool = False,
    ) -> None:
        _ = gobby_home
        assert clear_credential_rotation is True
        captured["database_url"] = database_url
        raise BootstrapConfigError("disk full")

    monkeypatch.setattr(datastores, "write_postgres_defaults", _fail_write)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert len(connection.statements) == 1
    new_url = captured["database_url"]
    password = unquote(cast(str, urlsplit(new_url).password))
    assert connection.statements[0].endswith(f"'{_FAKE_SCRAM}'")
    # The repair DSN is emitted redacted; the raw password never reaches output.
    assert password not in result.output
    assert "Set database_url in" in result.output
    assert "gobby:****@localhost:60891/gobby" in result.output
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


def test_postgres_rotation_publishes_pending_before_alter(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """A durable pending old/new pair exists before ALTER, and finalize clears it."""
    connection, connects = _patch_connect(monkeypatch)
    seen: list[dict[str, Any]] = []

    original_execute = connection.execute

    async def _observe(query: Any) -> None:
        seen.append(dict(read_bootstrap_yaml(rotation_home / "bootstrap.yaml")))
        await original_execute(query)

    monkeypatch.setattr(connection, "execute", _observe)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 0, result.output
    assert seen, "ALTER never ran"
    pending = seen[0].get("credential_rotation")
    assert isinstance(pending, dict), "no pending pair was durable before ALTER"
    assert pending["role"] == "gobby"
    _final_url, final_password = _new_password(rotation_home)
    assert pending["pending_password"] == final_password
    assert pending["previous_password"] == "old-secret"
    assert "credential_rotation" not in read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    _assert_single_bounded_connect(connects)


def test_startup_never_mutates_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A connect failure during startup leaves the bootstrap byte-identical."""
    import logging

    import gobby.storage.maintenance_epoch as maintenance_epoch
    from gobby.config.postgres_pool import DEFAULT_POSTGRES_POOL_CONFIG
    from gobby.runner_init import helpers

    home = tmp_path / "home"
    (home / "files").mkdir(parents=True)
    monkeypatch.setattr(datastores, "get_gobby_home", lambda: home)
    write_bootstrap_yaml(
        home / "bootstrap.yaml",
        {
            "datastore_mode": "local",
            "files_home": str(home / "files"),
            "database_url": _CURRENT_DSN,
        },
    )
    before = (home / "bootstrap.yaml").read_bytes()

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(maintenance_epoch, "_connect", _fail)
    config = SimpleNamespace(database_url=_CURRENT_DSN, postgres_pool=DEFAULT_POSTGRES_POOL_CONFIG)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(psycopg.OperationalError, match="connection refused"):
            helpers.init_hub_database(config)

    assert (home / "bootstrap.yaml").read_bytes() == before
    messages = [record.getMessage() for record in caplog.records]
    assert any("credentials were not modified" in message for message in messages)
    assert any("rotate-password postgres" in message for message in messages)
    assert all(_CURRENT_DSN not in message for message in messages)


def test_postgres_rotation_resume_reapplies_the_same_password(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """A pending pair makes the next run converge on the SAME intended password."""
    connection, connects = _patch_connect(monkeypatch)
    pending_password = "resume-intended-password-0123456789"
    update_bootstrap_yaml(
        rotation_home / "bootstrap.yaml",
        lambda data: data.__setitem__(
            "credential_rotation",
            {
                "role": "gobby",
                "pending_password": pending_password,
                "previous_password": "old-secret",
            },
        ),
    )

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 0, result.output
    _assert_single_bounded_connect(connects)
    assert connection.pgconn.encrypted_passwords == [pending_password]
    assert "credential_rotation" not in read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    _url, published = _new_password(rotation_home)
    assert published == pending_password
    assert pending_password not in result.output


def test_postgres_rotation_preserves_pending_on_indeterminate_commit(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """An unobserved COMMIT never finalizes and never claims rollback."""

    def _indeterminate(*_args: object, **_kwargs: object) -> None:
        raise IndeterminateCommitError("COMMIT outcome unobserved")

    monkeypatch.setattr(datastores, "_observe_hub_alter", _indeterminate)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert "phase=alter" in result.output
    assert "pending pair is preserved" in result.output
    pending = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["credential_rotation"]
    assert pending["role"] == "gobby"
    assert read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["database_url"] == _CURRENT_DSN
    assert pending["pending_password"] not in result.output


def test_postgres_rotation_resume_falls_back_to_intended_password_after_commit(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """A pending pair whose COMMIT already landed retries with the intended password."""
    pending_password = "resume-intended-password-0123456789"
    update_bootstrap_yaml(
        rotation_home / "bootstrap.yaml",
        lambda data: data.__setitem__(
            "credential_rotation",
            {
                "role": "gobby",
                "pending_password": pending_password,
                "previous_password": "old-secret",
            },
        ),
    )
    issued: list[str] = []

    async def _connect(dsn: str, **_kwargs: Any) -> _FakeConnection:
        issued.append(dsn)
        if dsn == _CURRENT_DSN:
            # psycopg's connect path raises the BASE OperationalError for a real
            # password refusal; the SQLSTATE subclass is never produced there.
            raise psycopg.OperationalError(
                'connection failed: FATAL:  password authentication failed for user "gobby"'
            )
        return _FakeConnection()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 0, result.output
    assert len(issued) == 2, issued
    assert issued[0] == _CURRENT_DSN
    fallback_pw = unquote(urlsplit(issued[1]).password or "")
    assert fallback_pw == pending_password
    assert "credential_rotation" not in read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    _url, published = _new_password(rotation_home)
    assert published == pending_password
    assert pending_password not in result.output


def test_postgres_rotation_resume_retries_intended_dsn_once_then_propagates(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """A resumed rotation retries the intended DSN once, then propagates the failure."""
    update_bootstrap_yaml(
        rotation_home / "bootstrap.yaml",
        lambda data: data.__setitem__(
            "credential_rotation",
            {
                "role": "gobby",
                "pending_password": "resume-intended-password-0123456789",
                "previous_password": "old-secret",
            },
        ),
    )
    issued: list[str] = []

    async def _connect(dsn: str, **_kwargs: Any) -> _FakeConnection:
        issued.append(dsn)
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert len(issued) == 2, issued
    assert issued[0] == _CURRENT_DSN
    fallback_pw = unquote(urlsplit(issued[1]).password or "")
    assert fallback_pw == "resume-intended-password-0123456789"
    assert "PostgreSQL password rotation failed: OperationalError" in result.output
    pending = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["credential_rotation"]
    assert pending["pending_password"] == "resume-intended-password-0123456789"


def test_postgres_rotation_resume_keeps_pending_when_fallback_also_fails(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """An ambiguous refusal whose intended-DSN fallback also fails never publishes."""
    pending_password = "resume-intended-password-0123456789"
    update_bootstrap_yaml(
        rotation_home / "bootstrap.yaml",
        lambda data: data.__setitem__(
            "credential_rotation",
            {
                "role": "gobby",
                "pending_password": pending_password,
                "previous_password": "old-secret",
            },
        ),
    )
    issued: list[str] = []

    async def _connect(dsn: str, **_kwargs: Any) -> _FakeConnection:
        issued.append(dsn)
        raise psycopg.OperationalError("connection failed: connection refused")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert len(issued) == 2, issued
    assert issued[0] == _CURRENT_DSN
    assert unquote(urlsplit(issued[1]).password or "") == pending_password
    # Failure proves neither outcome: the pending pair must survive and no new DSN is published.
    assert "credential_rotation" in read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    assert read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["database_url"] == _CURRENT_DSN
    assert pending_password not in result.output


def test_postgres_rotation_resume_bounded_timeout_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """A bounded-helper deadline is not the connect ambiguity, so it never retries."""
    pending_password = "resume-intended-password-0123456789"
    update_bootstrap_yaml(
        rotation_home / "bootstrap.yaml",
        lambda data: data.__setitem__(
            "credential_rotation",
            {
                "role": "gobby",
                "pending_password": pending_password,
                "previous_password": "old-secret",
            },
        ),
    )
    issued: list[str] = []

    async def _connect(dsn: str, **_kwargs: Any) -> _FakeConnection:
        issued.append(dsn)
        raise BoundedDBTimeoutError("bounded PostgreSQL work deadline expired")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", _connect)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert issued == [_CURRENT_DSN]
    pending = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["credential_rotation"]
    assert pending["pending_password"] == pending_password


def test_postgres_rotation_preserves_pending_on_observed_commit_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """An observed COMMIT cleanup failure keeps the durable recovery pair."""
    connection, _connects = _patch_connect(monkeypatch, fail_close=True)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1, result.output
    assert connection.committed is True
    bootstrap = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    assert bootstrap["database_url"] == _CURRENT_DSN
    pending = bootstrap["credential_rotation"]
    assert pending["previous_password"] == "old-secret"
    assert pending["pending_password"]
    assert "phase=cleanup" in result.output
    assert "COMMIT was observed" in result.output
    assert "rotate-password postgres" in result.output
    assert pending["pending_password"] not in result.output
    assert "old-secret" not in result.output
    assert _CURRENT_DSN not in result.output
    assert _FAKE_SCRAM not in result.output


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://gobby:old-secret@localhost:60891/gobby?user=evil",
        "postgresql://gobby:old-secret@localhost:60891/gobby?dbname=evil",
        "postgresql://gobby:old-secret@localhost:60891/gobby?host=evil.example",
        "postgresql://gobby:old-secret@localhost:60891/gobby?service=evil",
        "postgresql://gobby:old-secret@localhost:60891/gobby?passfile=evil",
    ],
)
def test_postgres_rotation_rejects_query_indirection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_url: str
) -> None:
    """Connection-string aliases that redirect identity or endpoint are refused."""
    monkeypatch.setattr(datastores, "get_gobby_home", lambda: tmp_path)
    _write_bootstrap(tmp_path)
    update_bootstrap_yaml(
        tmp_path / "bootstrap.yaml",
        lambda data: data.__setitem__("database_url", database_url),
    )

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no connection may be attempted for an indirect target")

    monkeypatch.setattr(datastores, "_observe_hub_alter", _refuse)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert "phase=validate" in result.output
    assert "old-secret" not in result.output


def test_postgres_rotation_preserves_unrelated_bootstrap_fields(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """Finalization preserves unrelated keys while dropping only the pending pair."""
    _patch_connect(monkeypatch)

    def _annotate(data: dict[str, Any]) -> None:
        data["ui_expose"] = "tailscale"
        data["cosmetic_note"] = "keep me"

    update_bootstrap_yaml(rotation_home / "bootstrap.yaml", _annotate)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 0, result.output
    persisted = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    assert persisted["cosmetic_note"] == "keep me"
    assert persisted["ui_expose"] == "tailscale"
    assert "credential_rotation" not in persisted
