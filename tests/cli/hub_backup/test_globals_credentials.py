"""Disposable-PostgreSQL proof that hub restore keeps the destination's credentials.

Opt-in with ``GOBBY_HUB_BACKUP_PG_SMOKE=1``. Each run starts one labelled scratch
container on a random loopback port and routes every product Docker command to
it, so neither the managed hub nor the shared test hub is reachable.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from gobby.cli.hub_backup import _stores as stores
from gobby.cli.hub_backup import _verify as verify
from gobby.storage.managed_credential_types import AUTH_SCHEMA

pytestmark = pytest.mark.integration

_SMOKE_ENV = "GOBBY_HUB_BACKUP_PG_SMOKE"
_PROTECTED_PORTS = frozenset({60891, 60892})
_READY_TIMEOUT_SECONDS = 120
_DOCKER_TIMEOUT_SECONDS = 120

Runner = Callable[..., subprocess.CompletedProcess[Any]]


@dataclass(frozen=True)
class _ScratchHub:
    container: verify.DisposableContainer
    port: int
    bootstrap_password: str

    def dsn(self, password: str) -> str:
        return f"postgresql://gobby:{password}@127.0.0.1:{self.port}/gobby"


def _scratch_only_run(real_run: Runner, container_id: str) -> Runner:
    """Forward Docker commands naming the scratch container; refuse anything else."""

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        assert args[0] == "docker" and container_id in args, f"non-scratch command: {args}"
        return real_run(args, **kwargs)

    return run


def _accepts_login(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except psycopg.OperationalError:
        return False


@pytest.fixture
def scratch_hub(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ScratchHub]:
    if os.environ.get(_SMOKE_ENV) != "1":
        pytest.skip(f"set {_SMOKE_ENV}=1 to run the disposable PostgreSQL credential proof")
    if shutil.which("docker") is None:
        pytest.fail("docker is required for the disposable PostgreSQL credential proof")
    real_run: Runner = subprocess.run
    password = secrets.token_urlsafe(24)
    name = f"gobby-hub-credential-test-{uuid.uuid4().hex[:8]}"
    run_id = uuid.uuid4().hex
    started = real_run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--label",
            f"{verify._DISPOSABLE_LABEL}=true",
            "--label",
            f"{verify._DISPOSABLE_RUN_LABEL}={run_id}",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            "POSTGRES_USER=gobby",
            "-e",
            "POSTGRES_DB=gobby",
            "-p",
            "127.0.0.1::5432",
            verify.POSTGRES_VERIFY_IMAGE,
            "postgres",
            "-c",
            "shared_preload_libraries=pg_search,pgaudit",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    container = verify.DisposableContainer(
        name=name, container_id=started.stdout.strip(), run_id=run_id
    )
    # Product code and cleanup alike may reach Docker only for this container.
    monkeypatch.setattr(subprocess, "run", _scratch_only_run(real_run, container.container_id))
    try:
        mapped = real_run(
            ["docker", "port", container.container_id, "5432/tcp"],
            capture_output=True,
            text=True,
            check=True,
            timeout=_DOCKER_TIMEOUT_SECONDS,
        )
        port = int(mapped.stdout.splitlines()[0].rsplit(":", 1)[1])
        assert port not in _PROTECTED_PORTS, f"scratch PostgreSQL mapped onto protected {port}"
        hub = _ScratchHub(container=container, port=port, bootstrap_password=password)
        assert verify._poll(
            lambda: _accepts_login(hub.dsn(password)), timeout=_READY_TIMEOUT_SECONDS
        ), "scratch PostgreSQL never accepted a TCP login"
        monkeypatch.setattr(
            stores, "_managed_postgres_container", lambda _dsn: container.container_id
        )
        yield hub
    finally:
        verify._remove_container(container)


def _dump_with_role_passwords(container: str, destination: Path) -> Path:
    """Write the globals artifact earlier hub backups produced, verifiers included."""
    dumped = subprocess.run(
        ["docker", "exec", container, "pg_dumpall", "-U", "gobby", "--globals-only"],
        capture_output=True,
        check=True,
        timeout=_DOCKER_TIMEOUT_SECONDS,
    )
    destination.write_bytes(dumped.stdout)
    return destination


def test_restore_keeps_rotated_destination_credential(
    scratch_hub: _ScratchHub,
    tmp_path: Path,
) -> None:
    source_dsn = scratch_hub.dsn(scratch_hub.bootstrap_password)
    destination_password = secrets.token_urlsafe(24)
    destination_dsn = scratch_hub.dsn(destination_password)
    with psycopg.connect(source_dsn, autocommit=True) as conn:
        # The hub's drain entry point; a scratch hub has no managed principals.
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(AUTH_SCHEMA)))
        conn.execute(
            sql.SQL(
                "CREATE FUNCTION {}.drain_ephemeral_principals() RETURNS integer "
                "LANGUAGE sql AS 'SELECT 0'"
            ).format(sql.Identifier(AUTH_SCHEMA))
        )
        conn.execute(
            "CREATE ROLE gobby_ops LOGIN CONNECTION LIMIT 3 "
            "VALID UNTIL '2099-01-01 00:00:00+00' PASSWORD 'ops-at-backup-time'"
        )
    backup_root = tmp_path / "backup"
    stores.dump_postgres(source_dsn, backup_root)
    current_globals = backup_root / stores.GLOBALS_DUMP_RELPATH
    legacy_globals = _dump_with_role_passwords(
        scratch_hub.container.container_id, tmp_path / "legacy-globals.sql"
    )
    assert b"SCRAM-SHA-256" in legacy_globals.read_bytes()

    with psycopg.connect(source_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER ROLE gobby PASSWORD {}").format(sql.Literal(destination_password))
        )

    for globals_path in (current_globals, legacy_globals):
        with psycopg.connect(destination_dsn, autocommit=True) as conn:
            conn.execute("DROP ROLE IF EXISTS gobby_ops")
        stores.restore_postgres_globals(destination_dsn, globals_path)

        assert stores.reconcile_restored_principals(destination_dsn) == 0
        with psycopg.connect(destination_dsn) as conn:
            restored = conn.execute(
                "SELECT rolcanlogin, rolconnlimit, rolvaliduntil::text, rolpassword IS NULL "
                "FROM pg_authid WHERE rolname = 'gobby_ops'"
            ).fetchone()
        assert restored == (True, 3, "2099-01-01 00:00:00+00", True), globals_path.name

    assert b"PASSWORD" not in current_globals.read_bytes()
