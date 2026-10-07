"""`gobby datastores rotate-password` contracts."""

from __future__ import annotations

import os
import re
import secrets
import stat
import subprocess
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import click
import psycopg
import pytest
from click.testing import CliRunner
from psycopg.pq import TransactionStatus

import gobby.cli.datastores as datastores
import gobby.cli.installers.falkor as falkor
from gobby.cli import cli
from gobby.cli.installers.managed_services_lock import (
    ManagedServicesLockError,
    managed_services_lock,
)
from gobby.config.bootstrap import BootstrapConfigError
from gobby.config.bootstrap_io import (
    read_bootstrap_yaml,
    update_bootstrap_yaml,
    write_bootstrap_yaml,
)
from gobby.config.persistence import validate_falkordb_password
from gobby.config.postgres_bootstrap import write_postgres_defaults
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.hub.async_ops import BoundedDBTimeoutError, IndeterminateCommitError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.secrets import SecretStore
from gobby.utils.durable_file import exclusive_file_lock
from tests.fixtures.fake_hub import fake_database_url

pytestmark = pytest.mark.unit

_CURRENT_DSN = fake_database_url("old-secret")
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
        self.info = SimpleNamespace(
            user="gobby",
            dbname="gobby_fixture",
            host="localhost",
            port=1,
            transaction_status=TransactionStatus.INTRANS,
        )

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
        1,
        "/gobby_fixture",
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
    assert "phase=alter" in result.output
    assert "rotate-password postgres" in result.output
    assert read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["database_url"] == _CURRENT_DSN
    assert "credential_rotation" in read_bootstrap_yaml(rotation_home / "bootstrap.yaml")


@pytest.mark.parametrize("after_rename", [False, True])
def test_postgres_rotation_prepare_failure_has_redacted_resume_guidance(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path, after_rename: bool
) -> None:
    connection, connects = _patch_connect(monkeypatch)
    replace = os.replace

    def fail_preparation(source: Any, destination: Any) -> None:
        if Path(destination) == rotation_home / "bootstrap.yaml":
            if after_rename:
                replace(source, destination)
            raise OSError("old-secret preparation fault")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_preparation)
    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])
    assert result.exit_code == 1
    assert connects == []
    assert connection.committed is False
    assert "phase=prepare" in result.output
    assert "OSError" in result.output
    assert "rotate-password postgres" in result.output
    assert "old-secret" not in result.output
    assert "postgresql://" not in result.output
    data = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    assert data["database_url"] == _CURRENT_DSN
    assert ("credential_rotation" in data) is after_rename


def test_resume_requires_durable_pending_republication_before_alter(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    path = rotation_home / "bootstrap.yaml"
    pair = {"role": "gobby", "previous_password": "old-secret", "pending_password": "new-secret"}
    update_bootstrap_yaml(path, lambda data: data.update(credential_rotation=pair))
    connection, connects = _patch_connect(monkeypatch)
    replace = os.replace

    def fail_durable_publication(source: Any, destination: Any) -> None:
        if Path(destination) == path:
            raise OSError("pending durability unavailable")
        replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_durable_publication)
    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])
    assert result.exit_code == 1
    assert connects == []
    assert connection.committed is False
    assert "phase=prepare" in result.output
    assert "rotate-password postgres" in result.output
    assert read_bootstrap_yaml(path)["credential_rotation"] == pair


@pytest.mark.parametrize(
    "writer", ["full-map", "drop-pair", "change-url", "defaults", "remote", "remote-full-map"]
)
def test_pending_writers_cannot_change_credential_state(rotation_home: Path, writer: str) -> None:
    path = rotation_home / "bootstrap.yaml"
    pair = {"role": "gobby", "previous_password": "old-secret", "pending_password": "new-secret"}
    update_bootstrap_yaml(path, lambda data: data.update(credential_rotation=pair))
    before = path.read_bytes()

    def mutate(data: dict[str, Any]) -> None:
        if writer == "drop-pair":
            data.pop("credential_rotation")
        elif writer in ("remote", "remote-full-map"):
            data.update(datastore_mode="remote", hub_daemon_url="http://hub.example:60887")
        else:
            data["database_url"] = _CURRENT_DSN.replace("old-secret", "unexpected-secret")

    with pytest.raises(BootstrapConfigError, match="credential|pending"):
        if writer == "full-map":
            stale = read_bootstrap_yaml(path)
            stale.pop("credential_rotation")
            write_bootstrap_yaml(path, stale)
        elif writer == "remote-full-map":
            candidate = read_bootstrap_yaml(path)
            mutate(candidate)
            write_bootstrap_yaml(path, candidate)
        elif writer == "defaults":
            write_postgres_defaults(
                gobby_home=rotation_home,
                database_url=_CURRENT_DSN.replace("old-secret", "unexpected-secret"),
            )
        else:
            update_bootstrap_yaml(path, mutate)
    assert path.read_bytes() == before
    update_bootstrap_yaml(path, lambda data: data.update(services_bind_address="127.0.0.1"))
    after = read_bootstrap_yaml(path)
    assert after["credential_rotation"] == pair
    assert after["database_url"] == _CURRENT_DSN
    assert after["services_bind_address"] == "127.0.0.1"


def test_initial_pending_publication_requires_complete_local_pair(rotation_home: Path) -> None:
    path = rotation_home / "bootstrap.yaml"
    before = path.read_bytes()
    with pytest.raises(BootstrapConfigError, match="pending_password"):
        update_bootstrap_yaml(path, lambda data: data.update(credential_rotation={"role": "gobby"}))
    assert path.read_bytes() == before


def test_postgres_rotation_reports_resume_when_bootstrap_write_fails(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    connection, _connects = _patch_connect(monkeypatch)
    attempts = _install_publication_fault(monkeypatch, rotation_home, "before-rename")

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1
    assert len(connection.statements) == 1
    assert connection.statements[0].endswith(f"'{_FAKE_SCRAM}'")
    assert attempts[0] == 3
    assert "phase=finalize" in result.output
    assert "pending-restored" in result.output
    assert "rotate-password postgres" in result.output
    assert "postgresql://" not in result.output
    assert read_bootstrap_yaml(rotation_home / "bootstrap.yaml")["database_url"] == _CURRENT_DSN


def _install_publication_fault(
    monkeypatch: pytest.MonkeyPatch,
    home: Path,
    fault: str,
    *,
    restore_fault: str | None = None,
) -> list[int]:
    path = home / "bootstrap.yaml"
    attempts = [0]
    replace = os.replace
    fsync = os.fsync
    read_bytes = Path.read_bytes
    readback_failed = False

    def replace_file(source: Any, destination: Any) -> None:
        if Path(destination) == path:
            attempts[0] += 1
            if (attempts[0] == 2 and fault == "before-rename") or (
                attempts[0] == 3 and restore_fault == "before-rename"
            ):
                raise OSError("old-secret new-secret publication fault")
        replace(source, destination)
        if Path(destination) == path and attempts[0] == 2 and fault == "after-rename":
            raise OSError("old-secret new-secret publication fault")
        if Path(destination) == path and attempts[0] == 3 and restore_fault == "after-rename":
            raise OSError("old-secret new-secret restoration fault")

    def sync(fd: int) -> None:
        if (attempts[0] == 2 and fault == "directory-fsync") or (
            attempts[0] == 3 and restore_fault == "directory-fsync"
        ):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("old-secret new-secret directory fsync fault")
        fsync(fd)

    def read(candidate: Path) -> bytes:
        nonlocal readback_failed
        if candidate == path and attempts[0] == 2 and fault == "readback" and not readback_failed:
            readback_failed = True
            raise OSError("old-secret new-secret readback fault")
        if candidate == path and attempts[0] == 3 and restore_fault == "readback":
            raise OSError("old-secret new-secret restoration readback fault")
        return read_bytes(candidate)

    monkeypatch.setattr(os, "replace", replace_file)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "read_bytes", read)
    return attempts


@pytest.mark.parametrize("fault", ["before-rename", "after-rename", "directory-fsync", "readback"])
def test_postgres_rotation_restores_pending_after_publication_fault(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path, fault: str
) -> None:
    connection, _ = _patch_connect(monkeypatch)
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _size: "new-secret")
    attempts = _install_publication_fault(monkeypatch, rotation_home, fault)
    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])
    assert result.exit_code == 1
    assert connection.committed is True
    assert attempts[0] == 3
    data = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    assert data["database_url"] == _CURRENT_DSN
    assert data["credential_rotation"] == {
        "role": "gobby",
        "previous_password": "old-secret",
        "pending_password": "new-secret",
    }
    assert "phase=finalize" in result.output
    assert "pending-restored" in result.output
    assert "OSError" in result.output
    assert "rotate-password postgres" in result.output
    for secret in ("old-secret", "new-secret", _CURRENT_DSN, _FAKE_SCRAM):
        assert secret not in result.output


@pytest.mark.parametrize("fault", ["before-rename", "after-rename", "directory-fsync", "readback"])
@pytest.mark.parametrize(
    "restore_fault", ["before-rename", "after-rename", "directory-fsync", "readback"]
)
def test_postgres_rotation_reports_indeterminate_when_restore_fails(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path, fault: str, restore_fault: str
) -> None:
    connection, _ = _patch_connect(monkeypatch)
    monkeypatch.setattr(secrets, "token_urlsafe", lambda _size: "new-secret")
    attempts = _install_publication_fault(
        monkeypatch, rotation_home, fault, restore_fault=restore_fault
    )
    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])
    assert result.exit_code == 1
    assert connection.committed is True
    assert attempts[0] == 3
    assert "phase=finalize" in result.output
    assert "indeterminate-publication" in result.output
    assert "OSError" in result.output
    assert "rotate-password postgres" in result.output
    for claim in ("was not updated", "still on disk", "pending-restored"):
        assert claim not in result.output
    for secret in ("old-secret", "new-secret", _CURRENT_DSN, _FAKE_SCRAM):
        assert secret not in result.output


def test_postgres_rotation_reports_indeterminate_when_publication_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    connection, _ = _patch_connect(monkeypatch)
    attempts = _install_publication_fault(monkeypatch, rotation_home, "after-rename")
    read_bytes = Path.read_bytes

    def unreadable_after_rename(path: Path) -> bytes:
        if path == rotation_home / "bootstrap.yaml" and attempts[0] == 2:
            raise OSError("old-secret unreadable publication")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable_after_rename)
    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])
    assert result.exit_code == 1
    assert connection.committed is True
    assert attempts[0] == 2
    assert "indeterminate-publication" in result.output
    assert "restoration=BootstrapConfigError" in result.output
    assert "rotate-password postgres" in result.output
    assert "pending-restored" not in result.output
    assert "old-secret" not in result.output


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
            fake_database_url("old"),
            "gobby",
            fake_database_url("new%2Fpw"),
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

    monkeypatch.setattr(psycopg, "connect", _fail)
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
        f"{_CURRENT_DSN}?user=evil",
        f"{_CURRENT_DSN}?dbname=evil",
        f"{_CURRENT_DSN}?host=evil.example",
        f"{_CURRENT_DSN}?service=evil",
        f"{_CURRENT_DSN}?passfile=evil",
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


@pytest.mark.parametrize(
    "database_url",
    [
        _CURRENT_DSN + "?u%73er=other",
        _CURRENT_DSN + "?%64bname=other",
        _CURRENT_DSN + "?%68ost=other",
        _CURRENT_DSN + "?%68ostaddr=127.0.0.1",
        _CURRENT_DSN + "?%70ort=60892",
        _CURRENT_DSN + "?%70assword=query-secret",
        _CURRENT_DSN + "?%73ervice=other",
        _CURRENT_DSN + "?%70assfile=/unused/credentials",
        _CURRENT_DSN + "?options=-c%20role%3Dother",
        _CURRENT_DSN + "?%6Fptions=-c%20role%3Dother",
        _CURRENT_DSN.replace("localhost", "localhost,127.0.0.1"),
        _CURRENT_DSN.replace("localhost", "localhost%2C127.0.0.1"),
        _CURRENT_DSN.replace("localhost", "%2Funused%2Fsocket"),
        _CURRENT_DSN.replace("localhost", "%40socket"),
        _CURRENT_DSN.replace(":old-secret", ":"),
        _CURRENT_DSN + "#other-database",
        _CURRENT_DSN + "#",
        _CURRENT_DSN.replace("/gobby", "/go\tbby"),
        _CURRENT_DSN.replace("old-secret", "du?mmy"),
        _CURRENT_DSN.replace(":1/", ":not-a-port/"),
    ],
)
def test_postgres_rotation_rejects_effective_libpq_indirection(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path, database_url: str
) -> None:
    """Validation precedes any pending publication or connection for unsafe targets."""
    path = rotation_home / "bootstrap.yaml"
    update_bootstrap_yaml(path, lambda data: data.__setitem__("database_url", database_url))
    before = path.read_bytes()
    connection, connects = _patch_connect(monkeypatch)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1, result.output
    assert "phase=validate" in result.output
    assert path.read_bytes() == before
    assert connects == []
    assert connection.statements == []
    assert connection.pgconn.encrypted_passwords == []
    assert "old-secret" not in result.output
    assert "query-secret" not in result.output
    assert database_url not in result.output


@pytest.mark.parametrize(
    "variable,value",
    [
        ("PGHOSTADDR", "127.0.0.1"),
        ("PGSERVICE", "unused-service"),
        ("PGSERVICEFILE", "/unused/service.conf"),
        ("PGPASSFILE", "/unused/credentials"),
        ("PGOPTIONS", "-c role=other"),
        ("PGSYSCONFDIR", "/unused/service-directory"),
    ],
)
def test_postgres_rotation_rejects_environment_indirection(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path, variable: str, value: str
) -> None:
    """Ambient libpq selectors cannot redirect the explicitly validated rotation."""
    path = rotation_home / "bootstrap.yaml"
    before = path.read_bytes()
    connection, connects = _patch_connect(monkeypatch)

    with monkeypatch.context() as environment:
        environment.setenv(variable, value)
        result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1, result.output
    assert "phase=validate" in result.output
    assert path.read_bytes() == before
    assert connects == []
    assert connection.pgconn.encrypted_passwords == []
    assert value not in result.output
    assert "old-secret" not in result.output


@pytest.mark.parametrize(
    "field,value", [("user", "other"), ("dbname", "other"), ("host", "other"), ("port", 60892)]
)
def test_postgres_rotation_rejects_connected_target_mismatch(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path, field: str, value: str | int
) -> None:
    """The live connection identity is checked before generating or applying SCRAM."""
    connection, connects = _patch_connect(monkeypatch)
    setattr(connection.info, field, value)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 1, result.output
    assert "phase=validate" in result.output
    assert "rotate-password postgres" in result.output
    assert len(connects) == 1
    assert connection.statements == []
    assert connection.pgconn.encrypted_passwords == []
    assert connection.committed is False
    assert connection.closed is True
    bootstrap = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    assert bootstrap["database_url"] == _CURRENT_DSN
    pending = bootstrap["credential_rotation"]
    assert pending["previous_password"] == "old-secret"
    assert pending["pending_password"]
    assert pending["pending_password"] not in result.output
    assert "old-secret" not in result.output
    assert _CURRENT_DSN not in result.output


def test_postgres_rotation_uses_normalized_direct_target(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """Encoded direct URI components address the same libpq role and endpoint."""
    dsn = "postgresql://g%6Fbby:old-secret@local%68ost:1/g%6Fbby_fixture?sslmode=disable"
    update_bootstrap_yaml(
        rotation_home / "bootstrap.yaml", lambda data: data.__setitem__("database_url", dsn)
    )
    connection, connects = _patch_connect(monkeypatch)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 0, result.output
    assert len(connects) == 1
    assert connection.statements == [f"ALTER ROLE \"gobby\" PASSWORD '{_FAKE_SCRAM}'"]
    bootstrap = read_bootstrap_yaml(rotation_home / "bootstrap.yaml")
    assert "credential_rotation" not in bootstrap
    assert "sslmode=disable" in bootstrap["database_url"]
    assert "old-secret" not in result.output


def test_postgres_rotation_ignores_stale_bootstrap_argument(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """The locked canonical reread selects the credential used for this rotation."""
    path = rotation_home / "bootstrap.yaml"
    fresh_url = _CURRENT_DSN.replace("old-secret", "fresh-primary-secret")

    def read_then_change_primary(candidate: Path) -> dict[str, Any]:
        snapshot = read_bootstrap_yaml(candidate)
        if snapshot["database_url"] == _CURRENT_DSN:
            update_bootstrap_yaml(
                candidate, lambda data: data.__setitem__("database_url", fresh_url)
            )
        return snapshot

    monkeypatch.setattr(datastores, "read_bootstrap_yaml", read_then_change_primary)
    connection, connects = _patch_connect(monkeypatch)
    previous_passwords: list[str] = []
    original_execute = connection.execute

    async def observe_prepared_pair(query: Any) -> None:
        pending = read_bootstrap_yaml(path)["credential_rotation"]
        previous_passwords.append(pending["previous_password"])
        await original_execute(query)

    monkeypatch.setattr(connection, "execute", observe_prepared_pair)

    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])

    assert result.exit_code == 0, result.output
    assert connects[0][0] == fresh_url
    assert previous_passwords == ["fresh-primary-secret"]
    assert connection.committed
    assert "credential_rotation" not in read_bootstrap_yaml(path)


def test_postgres_rotation_serializes_canonical_writers(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """Other canonical and managed-service writers are excluded throughout ALTER."""
    path = rotation_home / "bootstrap.yaml"
    entered: list[str] = []

    def probe_services_lock() -> bool:
        try:
            with managed_services_lock(rotation_home, operation="test-probe", timeout=0.01):
                return True
        except ManagedServicesLockError:
            return False

    def observe_alter(*_args: object, **_kwargs: object) -> None:
        pending = read_bootstrap_yaml(path)["credential_rotation"]
        entered.append(pending["pending_password"])
        with pytest.raises(TimeoutError):
            with exclusive_file_lock(path, timeout_seconds=0.01):
                pass
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(probe_services_lock).result(timeout=1) is False

    monkeypatch.setattr(datastores, "_observe_hub_alter", observe_alter)

    datastores._rotate_postgres_password(rotation_home)

    assert len(entered) == 1
    assert "credential_rotation" not in read_bootstrap_yaml(path)
    with exclusive_file_lock(path, timeout_seconds=0.1):
        assert path.is_file()
    assert probe_services_lock() is True


def test_stale_full_map_writer_cannot_replace_rotated_credentials(
    monkeypatch: pytest.MonkeyPatch, rotation_home: Path
) -> None:
    """A pre-rotation whole-map snapshot cannot publish the old primary again."""
    path = rotation_home / "bootstrap.yaml"
    stale = read_bootstrap_yaml(path)
    _patch_connect(monkeypatch)
    result = CliRunner().invoke(cli, ["datastores", "rotate-password", "postgres"])
    assert result.exit_code == 0, result.output
    after_rotation = path.read_bytes()

    with pytest.raises(BootstrapConfigError, match="credential"):
        write_bootstrap_yaml(path, stale)

    assert path.read_bytes() == after_rotation
    current = read_bootstrap_yaml(path)
    assert current["database_url"] != _CURRENT_DSN
    assert "credential_rotation" not in current


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
