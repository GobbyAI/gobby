"""Safety checks for PostgreSQL test fixtures."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest
from _pytest.outcomes import Failed, Skipped
from psycopg import sql
from psycopg.conninfo import make_conninfo

from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.maintenance_epoch import abort_maintenance_epoch, open_maintenance_epoch
from tests.fixtures.postgres import (
    _ISOLATED_SCHEMA_APPLICATION_PREFIX,
    _adapt_seed_rows,
    _cleanup_orphaned_schemas,
    _enforce_safe_test_schema,
    _require_test_database_url,
    _schema_looks_test_only,
    isolated_test_schema,
    pytest_configure,
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


def _stub_bootstrap_hub(monkeypatch: pytest.MonkeyPatch, database_url: str | None) -> None:
    from gobby.config import bootstrap

    monkeypatch.setattr(
        bootstrap,
        "load_bootstrap",
        lambda **_kwargs: SimpleNamespace(database_url=database_url),
    )


def _configure_rejection(config: pytest.Config) -> str | None:
    """Return the guard's rejection message, or None when the run is allowed."""
    try:
        pytest_configure(config)
    except pytest.UsageError as exc:
        return str(exc)
    return None


def test_pytest_configure_rejects_database_url_pointing_at_the_live_hub(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
) -> None:
    _stub_bootstrap_hub(monkeypatch, "postgresql://gobby:pass@localhost:60891/gobby")
    # A different user and the loopback alias still reach the same database.
    monkeypatch.setenv("DATABASE_URL", "postgresql://other@127.0.0.1:60891/gobby")

    rejection = _configure_rejection(pytestconfig)

    assert rejection is not None
    assert "live Gobby hub database" in rejection


def test_pytest_configure_allows_an_isolated_test_database(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
) -> None:
    _stub_bootstrap_hub(monkeypatch, "postgresql://gobby:pass@localhost:60891/gobby")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test"
    )

    assert _configure_rejection(pytestconfig) is None


def test_pytest_configure_allows_runs_with_no_configured_hub(
    monkeypatch: pytest.MonkeyPatch,
    pytestconfig: pytest.Config,
) -> None:
    _stub_bootstrap_hub(monkeypatch, None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://gobby:pass@localhost:60891/gobby")

    assert _configure_rejection(pytestconfig) is None


def test_require_test_database_url_fails_loudly_under_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent with no test database must never silently bind the live hub."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GOBBY_TEST_PROTECT", "1")

    with pytest.raises(Failed, match="isolated PostgreSQL test database"):
        _require_test_database_url()


def test_require_test_database_url_skips_without_test_protect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GOBBY_TEST_PROTECT", raising=False)

    with pytest.raises(Skipped, match="isolated PostgreSQL test database"):
        _require_test_database_url()


def test_adapt_seed_rows_wraps_json_values() -> None:
    adapted = _adapt_seed_rows([({"mode": "auto"}, ["fast"], "plain")])

    assert adapted[0][2] == "plain"
    assert adapted[0][0].obj == {"mode": "auto"}
    assert adapted[0][1].obj == ["fast"]


def test_orphan_cleanup_delegates_to_leased_sweeper_with_live_process_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.fixtures.postgres as postgres_fixture

    calls: list[tuple[str, int]] = []

    def sweep(url: str, age_hours: int) -> int:
        calls.append((url, age_hours))
        return 0

    monkeypatch.setattr(postgres_fixture, "sweep_orphaned_test_schemas", sweep)

    _cleanup_orphaned_schemas("postgresql://test")

    assert calls == [("postgresql://test", 1)]


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
    connection.info.backend_pid = 12345
    connection.closed = False

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
    assert connect.call_args.kwargs["application_name"].startswith(
        f"{_ISOLATED_SCHEMA_APPLICATION_PREFIX}master-"
    )


@pytest.mark.integration
def test_isolated_test_schema_recovers_after_maintenance_epoch_terminates_backend(
    isolated_postgres_database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with psycopg.connect(isolated_postgres_database_url, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION pg_search")

    database = PostgresHubDatabase(isolated_postgres_database_url)
    try:
        database.apply_migrations()
    finally:
        database.close()

    caplog.set_level("INFO", logger="tests.fixtures.postgres")
    with isolated_test_schema(isolated_postgres_database_url, "epoch") as schema:
        epoch = open_maintenance_epoch(
            isolated_postgres_database_url,
            campaign="schema-apply",
            opened_by="test-isolated-schema-teardown",
            scope_note="terminate the fixture backend",
        )
        abort_maintenance_epoch(
            isolated_postgres_database_url,
            epoch.id,
            disposition="fixture resilience test completed",
            confirmed=True,
        )

    with psycopg.connect(isolated_postgres_database_url, autocommit=True) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                (schema,),
            ).fetchone()
            is None
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        _ISOLATED_SCHEMA_APPLICATION_PREFIX in message and "backend pid" in message
        for message in messages
    )
    assert any("lost PostgreSQL backend pid" in message for message in messages)


@pytest.mark.integration
def test_orphan_sweep_drops_recent_unleased_schema_and_preserves_live_schema(
    isolated_postgres_database_url: str,
) -> None:
    from gobby.runner_maintenance import storage_hygiene

    created_epoch = int(time.time())
    stale_schema = f"gobby_test_{created_epoch}_{os.getpid()}_stale_{uuid.uuid4().hex[:6]}"
    live_schema = f"gobby_test_{created_epoch}_{os.getpid()}_live_{uuid.uuid4().hex[:6]}"
    with psycopg.connect(isolated_postgres_database_url, autocommit=True) as live_connection:
        live_connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (live_schema,))
        live_connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(stale_schema)))
        live_connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(live_schema)))
        try:
            dropped = storage_hygiene.sweep_orphaned_test_schemas(
                isolated_postgres_database_url,
                age_hours=0,
            )

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
