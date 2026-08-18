"""Tests for atexit cleanup of leaked PostgreSQL hub connection pools."""

from __future__ import annotations

import gc
import subprocess
import sys
import weakref
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.hub import postgres
from gobby.storage.hub.postgres import PostgresHubDatabase

_DUMMY_DSN = "postgresql://gobby:secret@localhost/gobby"


@pytest.mark.unit
def test_new_database_is_tracked_for_atexit_close() -> None:
    db = PostgresHubDatabase(_DUMMY_DSN)
    assert db in postgres._OPEN_DATABASES

    db.close()

    assert db not in postgres._OPEN_DATABASES


@pytest.mark.unit
def test_close_caps_pool_worker_wait_within_daemon_shutdown_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = PostgresHubDatabase(_DUMMY_DSN)
    close_timeouts: list[float] = []

    def close_pool(*, timeout: float) -> None:
        close_timeouts.append(timeout)

    monkeypatch.setattr(db._pool, "close", close_pool)

    db.close()

    assert close_timeouts == [postgres._POOL_CLOSE_TIMEOUT_SECONDS]
    assert db._pool_closed is True
    assert db._pool_opened is False


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
        assert failing in postgres._OPEN_DATABASES
    finally:
        failing.close = original_failing_close  # type: ignore[method-assign]
        postgres._close_open_databases_at_exit()

    assert failing._pool_closed is True
    assert failing not in postgres._OPEN_DATABASES


@pytest.mark.unit
def test_function_local_leak_remains_reachable_for_atexit() -> None:
    def leak_database() -> weakref.ReferenceType[PostgresHubDatabase]:
        db = PostgresHubDatabase(_DUMMY_DSN)
        return weakref.ref(db)

    ref = leak_database()
    gc.collect()

    leaked = ref()
    assert leaked is not None
    assert leaked in postgres._OPEN_DATABASES
    leaked.close()


@pytest.mark.unit
def test_failed_close_remains_registered_and_can_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    db = PostgresHubDatabase(_DUMMY_DSN)
    close_calls = 0

    def close_pool(*, timeout: float) -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("temporary close failure")

    monkeypatch.setattr(db._pool, "close", close_pool)

    with pytest.raises(RuntimeError, match="temporary close failure"):
        db.close()

    assert db in postgres._OPEN_DATABASES
    assert db._pool_closed is False

    db.close()

    assert close_calls == 2
    assert db not in postgres._OPEN_DATABASES
    assert db._pool_closed is True


@pytest.mark.integration
def test_tasks_list_cli_exit_prints_no_finalization_noise(
    postgres_database_url: str,
    postgres_schema: str,
    postgres_canonical_seed: dict[str, list[tuple[Any, ...]]],
    tmp_path: Path,
) -> None:
    """A normal task CLI invocation closes its pool before Python finalization.

    The DSN handed to the subprocess is pinned to this worker's
    ``gobby_test_*`` schema so the CLI never reaches the live hub. The seed
    fixture already migrated that schema to head, so the CLI's own
    ``apply_migrations()`` finds nothing pending; an unscoped DSN would instead
    land on an already-baselined database and be refused by the destructive
    migration gate.
    """
    config_path = tmp_path / "bootstrap.yaml"
    scoped_url = f"{postgres_database_url}?options=-csearch_path%3D{postgres_schema}"
    files_home = tmp_path / "files"
    files_home.mkdir()
    config_path.write_text(
        f'hub_backend: postgres\ndatabase_url: "{scoped_url}"\nfiles_home: {files_home}\n',
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gobby.cli",
            "--config",
            str(config_path),
            "tasks",
            "list",
            "--active",
            "--limit",
            "1",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "PythonFinalizationError" not in result.stderr
    assert "Exception ignored in: <function ConnectionPool.__del__" not in result.stderr
