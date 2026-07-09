"""Tests for atexit cleanup of leaked PostgreSQL hub connection pools."""

from __future__ import annotations

import gc
import subprocess
import sys
import textwrap
import weakref

import pytest

from gobby.storage.hub import postgres
from gobby.storage.hub.postgres import PostgresHubDatabase

_DUMMY_DSN = "postgresql://gobby:secret@localhost/gobby"


@pytest.mark.unit
def test_new_database_is_tracked_for_atexit_close() -> None:
    db = PostgresHubDatabase(_DUMMY_DSN)
    try:
        assert db in postgres._OPEN_DATABASES
    finally:
        db.close()


@pytest.mark.unit
def test_atexit_sweep_closes_open_databases() -> None:
    db = PostgresHubDatabase(_DUMMY_DSN)

    postgres._close_open_databases_at_exit()

    assert db._pool_closed is True
    # Idempotent: a second sweep must not raise on already-closed pools.
    postgres._close_open_databases_at_exit()


@pytest.mark.unit
def test_atexit_sweep_continues_past_close_errors() -> None:
    failing = PostgresHubDatabase(_DUMMY_DSN)
    healthy = PostgresHubDatabase(_DUMMY_DSN)
    original_failing_close = failing.close
    failing.close = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]

    try:
        postgres._close_open_databases_at_exit()
        assert healthy._pool_closed is True
    finally:
        failing.close = original_failing_close  # type: ignore[method-assign]
        failing.close()
        healthy.close()


@pytest.mark.unit
def test_collected_database_leaves_tracking_set() -> None:
    db = PostgresHubDatabase(_DUMMY_DSN)
    db.close()
    ref = weakref.ref(db)

    del db
    gc.collect()

    assert ref() is None


@pytest.mark.integration
def test_cli_exit_without_close_prints_no_finalization_noise(
    postgres_database_url: str,
) -> None:
    """A leaked, opened pool must not spray PythonFinalizationError at exit.

    Reproduces the CLI leak shape: the db handle survives until interpreter
    shutdown (module global), so without the atexit sweep the pool reaches
    ConnectionPool.__del__ during finalization and Thread.join() raises on
    Python 3.14.
    """
    script = textwrap.dedent(
        """
        import sys

        from gobby.storage.hub.postgres import PostgresHubDatabase

        db = PostgresHubDatabase(sys.argv[1])
        row = db.fetchone("SELECT 1 AS one")
        assert row is not None and row["one"] == 1
        # Deliberately no db.close(): the atexit sweep must cover the leak.
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, postgres_database_url],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "PythonFinalizationError" not in result.stderr
    assert "psycopg_pool" not in result.stderr
    assert result.stderr.strip() == ""
