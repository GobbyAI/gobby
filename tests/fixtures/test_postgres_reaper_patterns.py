"""Every per-process test database name shape the fixture reaper covers."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests.fixtures.postgres import isolated_test_database
from tests.fixtures.test_postgres_fixture_reaper import _database_names, _dead_pid

pytestmark = [pytest.mark.unit, pytest.mark.integration]

# isolated_test_database (also the baseline-application tests) and the sweep tests'
# isolated_postgres_database_url fixture.
_SHAPES: dict[str, Callable[[int], str]] = {
    "isolated": lambda pid: f"gobby_test_{pid}_{uuid.uuid4().hex[:12]}",
    "sweep": lambda pid: f"gobby_sweep_test_{pid}_{uuid.uuid4().hex[:8]}",
}


@pytest.fixture
def maintenance_url(postgres_database_url: str) -> str:
    return make_conninfo(postgres_database_url, dbname="postgres")


@pytest.fixture
def created_databases(maintenance_url: str) -> Iterator[list[str]]:
    names: list[str] = []
    yield names
    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        for name in names:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


def _create(maintenance_url: str, created: list[str], names: list[str]) -> None:
    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created.append(name)


def test_reaper_drops_dead_pid_databases_of_each_shape(
    postgres_database_url: str, maintenance_url: str, created_databases: list[str]
) -> None:
    dead_pid = _dead_pid()
    dead = [shape(dead_pid) for shape in _SHAPES.values()]
    # Each prefix keeps its own hex length; crossed lengths are not ours to drop.
    crossed = [
        f"gobby_test_{dead_pid}_{uuid.uuid4().hex[:8]}",
        f"gobby_sweep_test_{dead_pid}_{uuid.uuid4().hex[:12]}",
    ]
    _create(maintenance_url, created_databases, [*dead, *crossed])

    with isolated_test_database(postgres_database_url):
        names = _database_names(maintenance_url)

    assert names.isdisjoint(dead)
    assert set(crossed) <= names


def test_reaper_keeps_live_pid_databases_of_each_shape(
    postgres_database_url: str, maintenance_url: str, created_databases: list[str]
) -> None:
    live = [shape(os.getpid()) for shape in _SHAPES.values()]
    _create(maintenance_url, created_databases, live)

    with isolated_test_database(postgres_database_url):
        names = _database_names(maintenance_url)

    assert set(live) <= names
