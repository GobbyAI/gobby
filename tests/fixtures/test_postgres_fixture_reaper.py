"""Dead-process database reaping in the PostgreSQL test fixtures."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests.fixtures.postgres import _reap_dead_process_databases, isolated_test_database

pytestmark = pytest.mark.unit


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    assert process.wait() == 0
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)
    return process.pid


def _database_names(maintenance_url: str) -> set[str]:
    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        rows = connection.execute("SELECT datname FROM pg_database").fetchall()
    return {name for (name,) in rows}


@pytest.mark.integration
def test_isolated_test_database_reaps_only_dead_pid_databases(postgres_database_url: str) -> None:
    maintenance_url = make_conninfo(postgres_database_url, dbname="postgres")
    dead_pid = _dead_pid()
    dead = f"gobby_test_{dead_pid}_{uuid.uuid4().hex[:12]}"
    live = f"gobby_test_{os.getpid()}_{uuid.uuid4().hex[:12]}"
    near_misses = [
        f"gobby_test_{dead_pid}_{uuid.uuid4().hex[:10]}",
        f"gobby_test_{dead_pid}_{uuid.uuid4().hex[:12]}_x",
        f"gobby_test_0_{uuid.uuid4().hex[:12]}",
    ]
    created = [dead, live, *near_misses]
    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        for name in created:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with isolated_test_database(postgres_database_url):
            names = _database_names(maintenance_url)
        assert dead not in names
        assert {live, *near_misses, "gobby_test"} <= names
    finally:
        with psycopg.connect(maintenance_url, autocommit=True) as connection:
            for name in created:
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


@pytest.mark.integration
def test_reaper_leaves_a_non_loopback_hub_alone(
    postgres_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A remote hub's clients do not share this machine's PID space. The database
    # carries this live PID so concurrent runs keep it, and the patched liveness
    # check calls it dead here, so only the loopback guard can keep it.
    monkeypatch.setattr("tests.fixtures.postgres._process_alive", lambda pid: False)
    maintenance_url = make_conninfo(postgres_database_url, dbname="postgres")
    orphan = f"gobby_test_{os.getpid()}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(orphan)))
        try:
            _reap_dead_process_databases(
                connection, "postgresql://u:p@db.example.com:5432/gobby_test"
            )
            assert orphan in _database_names(maintenance_url)
        finally:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(orphan))
            )
