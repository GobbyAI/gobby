"""Safety checks for PostgreSQL test fixtures."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests.fixtures.postgres import (
    _adapt_seed_rows,
    _cleanup_orphaned_schemas,
    _configured_postgres_database_url,
    _enforce_safe_test_schema,
    _schema_looks_test_only,
    isolated_test_schema,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_postgres_database_url(postgres_database_url: str) -> Iterator[str]:
    """Create a throwaway database so schema sweeps cannot reach the live hub."""
    database_name = f"gobby_sweep_test_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    maintenance_url = make_conninfo(postgres_database_url, dbname="postgres")
    isolated_url = make_conninfo(postgres_database_url, dbname=database_name)

    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        yield isolated_url
    finally:
        with psycopg.connect(maintenance_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database_name))
            )


def test_schema_looks_test_only_accepts_test_schema_name() -> None:
    assert _schema_looks_test_only("gobby_test_12345_1_master_abcd")


def test_schema_looks_test_only_rejects_default_public_schema() -> None:
    assert not _schema_looks_test_only("public")


def test_enforce_safe_test_schema_fails_under_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

    with pytest.raises(pytest.fail.Exception, match="outside a gobby_test_"):
        _enforce_safe_test_schema("public")


def test_enforce_safe_test_schema_allows_test_schema_under_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

    assert _enforce_safe_test_schema("gobby_test_12345_1_master_abcd") is None


def test_configured_postgres_database_url_requires_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

    assert _configured_postgres_database_url() is None


def test_configured_postgres_database_url_reads_bootstrap_under_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.config import bootstrap

    database_url = "postgresql://user:pass@localhost:60891/gobby"
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")
    monkeypatch.setattr(
        bootstrap,
        "load_bootstrap",
        lambda **_kwargs: SimpleNamespace(database_url=database_url),
    )

    assert _configured_postgres_database_url() == database_url


def test_adapt_seed_rows_wraps_json_values() -> None:
    adapted = _adapt_seed_rows([({"mode": "auto"}, ["fast"], "plain")])

    assert adapted[0][2] == "plain"
    assert adapted[0][0].obj == {"mode": "auto"}
    assert adapted[0][1].obj == ["fast"]


def test_orphan_cleanup_delegates_to_leased_sweeper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.fixtures.postgres as postgres_fixture

    calls: list[tuple[str, int]] = []

    def sweep(url: str, age_hours: int) -> int:
        calls.append((url, age_hours))
        return 0

    monkeypatch.setattr(postgres_fixture, "sweep_orphaned_test_schemas", sweep)

    _cleanup_orphaned_schemas("postgresql://test", age_hours=12)

    assert calls == [("postgresql://test", 12)]


def test_isolated_test_schema_rejects_label_that_breaks_name_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid worker label reached PostgreSQL setup")

    monkeypatch.setattr("tests.fixtures.postgres._cleanup_orphaned_schemas", unexpected_call)
    monkeypatch.setattr("tests.fixtures.postgres.psycopg.connect", unexpected_call)

    with pytest.raises(ValueError, match="worker label"):
        with isolated_test_schema("postgresql://test", "gw_0"):
            pass


def test_isolated_test_schema_holds_schema_lease_for_fixture_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.fixtures.postgres as postgres_fixture

    events: list[str] = []
    lock_params: list[object] = []
    connection = MagicMock()
    connection.__enter__.return_value = connection

    def execute(query: object, params: object | None = None) -> MagicMock:
        rendered = str(query)
        result = MagicMock()
        if "pg_advisory_unlock" in rendered:
            events.append("unlock")
        elif "pg_advisory_lock" in rendered:
            events.append("lease")
            lock_params.append(params)
        elif "CREATE SCHEMA" in rendered:
            events.append("create")
        elif "information_schema.schemata" in rendered:
            result.fetchone.return_value = (1,)
        elif "DROP SCHEMA" in rendered:
            events.append("drop")
        return result

    connection.execute.side_effect = execute
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(postgres_fixture, "_cleanup_orphaned_schemas", MagicMock())
    monkeypatch.setattr("tests.fixtures.postgres.psycopg.connect", connect)

    with isolated_test_schema("postgresql://test", "master") as schema:
        assert events == ["lease", "create"]
        assert connection.__exit__.call_count == 0
        assert lock_params == [(schema,)]

    assert events == ["lease", "create", "drop", "unlock"]
    assert connection.__exit__.call_count == 1
    assert connect.call_count == 1


@pytest.mark.integration
def test_orphan_sweep_drops_stale_schema_and_preserves_live_schema(
    isolated_postgres_database_url: str,
) -> None:
    from gobby.runner_maintenance import storage_hygiene

    stale_schema = f"gobby_test_0_{os.getpid()}_stale_{uuid.uuid4().hex[:6]}"
    live_schema = f"gobby_test_0_{os.getpid()}_live_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(isolated_postgres_database_url, autocommit=True) as live_connection:
        live_connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (live_schema,))
        live_connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(stale_schema)))
        live_connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(live_schema)))
        try:
            dropped = storage_hygiene.sweep_orphaned_test_schemas(isolated_postgres_database_url)

            assert dropped == 1
            assert (
                live_connection.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                    (stale_schema,),
                ).fetchone()
                is None
            )
            assert live_connection.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (live_schema,),
            ).fetchone() == (1,)
        finally:
            live_connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(stale_schema))
            )
            live_connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(live_schema))
            )
            live_connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (live_schema,))
