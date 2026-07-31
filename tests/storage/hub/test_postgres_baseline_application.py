from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from contextlib import contextmanager
from types import ModuleType
from typing import Any

import pytest

from gobby.config.postgres_pool import PostgresPoolConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.migrations import BASELINE_VERSION, MigrationUnsupportedError

pytestmark = pytest.mark.unit


def _postgres_module() -> ModuleType:
    return importlib.import_module("gobby.storage.hub.postgres")


class _Result:
    def __init__(self, rows=()) -> None:
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _ClassifyConnection:
    def __init__(self, tables: set[str], baseline_versions: set[int] | None = None) -> None:
        self.tables = tables
        self.baseline_versions = baseline_versions or set()

    def execute(self, sql: str, params=()):
        if "pg_tables" in sql:
            return _Result([(table,) for table in sorted(self.tables)])
        if "MAX(version)" in sql:
            max_version = max(self.baseline_versions) if self.baseline_versions else None
            return _Result([(max_version,)])
        if "schema_migrations" in sql:
            version = BASELINE_VERSION if not params else params[0]
            return _Result([(1,)] if version in self.baseline_versions else [])
        raise AssertionError(f"unexpected query: {sql}")


@pytest.mark.parametrize(
    ("tables", "versions", "expected"),
    [
        (set(), set(), "fresh"),
        ({"gobby_install_ownership"}, set(), "fresh_with_install_infra"),
        ({"schema_migrations", "tasks"}, {BASELINE_VERSION}, "already_baselined"),
        ({"schema_migrations", "tasks"}, {BASELINE_VERSION + 1}, "already_baselined"),
        ({"schema_migrations", "tasks"}, {BASELINE_VERSION - 1}, "corrupt_partial"),
        ({"schema_migrations"}, set(), "fresh"),
        ({"tasks"}, set(), "corrupt_partial"),
        ({"gobby_install_ownership", "tasks"}, set(), "corrupt_partial"),
        (
            {
                "code_indexed_projects",
                "code_indexed_files",
                "code_symbols",
                "code_imports",
                "code_calls",
                "code_content_chunks",
            },
            set(),
            "gcore_code_index",
        ),
        (
            {
                "code_indexed_projects",
                "code_indexed_files",
                "code_symbols",
                "code_imports",
                "code_calls",
                "code_content_chunks",
                "code_index_future_table",
            },
            set(),
            "gcore_code_index",
        ),
        (
            {"gwiki_documents", "gwiki_chunks", "gwiki_sources"},
            set(),
            "gwiki_standalone",
        ),
        (
            {
                "code_indexed_projects",
                "code_indexed_files",
                "code_symbols",
                "code_imports",
                "code_calls",
                "code_content_chunks",
                "gwiki_documents",
                "gwiki_chunks",
                "gwiki_sources",
            },
            set(),
            "gcore_code_index",
        ),
        ({"code_symbols"}, set(), "corrupt_partial"),
        ({"gwiki_documents", "tasks"}, set(), "corrupt_partial"),
    ],
)
def test_classify_baseline_state_distinguishes_fresh_infra_and_corruption(
    tables,
    versions,
    expected,
) -> None:
    module = _postgres_module()

    assert module._classify_baseline_state(_ClassifyConnection(tables, versions)) == expected


class _ConnectionContext:
    def __init__(self, conn) -> None:
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _ApplyConnection:
    def __init__(
        self,
        state: str,
        *,
        pg_search_present: bool = True,
        pgcrypto_present: bool = True,
        columns: dict[str, set[str]] | None = None,
        max_version: int | None = None,
        tables: set[str] | None = None,
    ) -> None:
        self.state = state
        self.pg_search_present = pg_search_present
        self.pgcrypto_present = pgcrypto_present
        self.max_version = max_version
        self.tables = tables or set()
        module = _postgres_module()
        contracts = {**module._GCORE_CODE_INDEX_COLUMNS, **module._GWIKI_COLUMNS}
        self.columns = (
            {table: set(expected) for table, expected in contracts.items()}
            if columns is None
            else columns
        )
        self.statements: list[str] = []
        self.transaction_entered = False
        self.transaction_exited = False

    @contextmanager
    def transaction(self):
        self.transaction_entered = True
        try:
            yield self
        finally:
            self.transaction_exited = True

    def execute(self, sql: str, params=()):
        rendered = f"{sql} {params!r}" if params else sql
        self.statements.append(rendered)
        extension = params[0] if params else None
        if "pg_extension" in sql and extension == "pg_search":
            return _Result([(1,)] if self.pg_search_present else [])
        if "pg_extension" in sql and extension == "pgcrypto":
            return _Result([(1,)] if self.pgcrypto_present else [])
        if "MAX(version)" in sql:
            return _Result([(self.max_version,)])
        if "pg_tables" in sql:
            return _Result([(table,) for table in sorted(self.tables)])
        if "information_schema.columns" in sql:
            requested_tables = params[0]
            return _Result(
                [
                    (table, column)
                    for table in requested_tables
                    for column in sorted(self.columns.get(table, set()))
                ]
            )
        return _Result()


class _Pool:
    def __init__(self, *connections: _ApplyConnection) -> None:
        self._connections = list(connections)

    def connection(self):
        return _ConnectionContext(self._connections.pop(0))


class _Resources:
    def __init__(self) -> None:
        self.read_count = 0

    def files(self, package: str):
        assert package == "gobby.storage"
        return self

    def joinpath(self, name: str):
        assert name == "postgres_baseline_schema.sql"
        return self

    def read_text(self) -> str:
        self.read_count += 1
        return "CREATE TABLE tasks(id INTEGER);"


class _GcoreAdoptionResources(_Resources):
    def read_text(self) -> str:
        self.read_count += 1
        return """
CREATE TABLE code_symbols(id TEXT PRIMARY KEY);
CREATE INDEX idx_cs_project ON code_symbols(project_id);
CREATE INDEX code_symbols_search_bm25 ON code_symbols
USING bm25 (id, name)
WITH (key_field='id');
CREATE TABLE gwiki_documents(id TEXT PRIMARY KEY);
CREATE TABLE gwiki_chunks(id TEXT PRIMARY KEY, document_id TEXT);
CREATE INDEX idx_gwiki_chunks_document ON gwiki_chunks(document_id);
CREATE TABLE gwiki_sources(id TEXT PRIMARY KEY);
CREATE TABLE tasks(id INTEGER);
"""


class _GwikiAdoptionResources(_Resources):
    def read_text(self) -> str:
        self.read_count += 1
        return """
CREATE TABLE gwiki_documents(id TEXT PRIMARY KEY);
CREATE TABLE gwiki_chunks(id TEXT PRIMARY KEY, document_id TEXT);
CREATE INDEX idx_gwiki_chunks_document ON gwiki_chunks(document_id);
CREATE TABLE gwiki_sources(id TEXT PRIMARY KEY);
CREATE TABLE tasks(id INTEGER);
"""


def _new_db(module, pool: _Pool):
    db = object.__new__(module.PostgresHubDatabase)
    db._pool = pool
    return db


def test_postgres_pool_opens_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construction sets open=False and only explicit open() opens the pool once."""
    module = _postgres_module()
    calls: dict[str, object] = {}
    monkeypatch.delenv("PGAPPNAME", raising=False)
    monkeypatch.setenv("PGPOOL_MIN", "99")
    monkeypatch.setenv("PGPOOL_MAX", "100")
    monkeypatch.setenv("PGPOOL_TIMEOUT", "101")
    monkeypatch.setenv("PGPOOL_OPEN_TIMEOUT", "102")
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "99")

    class FakePool:
        def __init__(self, *args, **kwargs) -> None:
            calls["constructor_open"] = kwargs["open"]
            calls["constructor_min_size"] = kwargs["min_size"]
            calls["constructor_max_size"] = kwargs["max_size"]
            calls["constructor_timeout"] = kwargs["timeout"]
            calls["pool_kwargs"] = kwargs["kwargs"]

        def open(self, *, wait: bool, timeout: float) -> None:
            calls["opened"] = (wait, timeout)

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(module, "ConnectionPool", FakePool)

    db = module.PostgresHubDatabase("postgresql://gobby:secret@localhost/gobby")
    assert calls["constructor_open"] is False
    assert calls["constructor_min_size"] == 2
    assert calls["constructor_max_size"] == 20
    assert calls["constructor_timeout"] == 5.0
    pool_kwargs = calls["pool_kwargs"]
    assert isinstance(pool_kwargs, dict)
    assert pool_kwargs["application_name"].startswith("gobby-hub-")
    assert pool_kwargs["prepare_threshold"] is None
    assert pool_kwargs["row_factory"] is module.dict_row
    assert "opened" not in calls

    db.open(timeout=1.5)
    db.open(timeout=9.0)

    assert calls["opened"] == (True, 1.5)


def test_postgres_pool_uses_injected_config(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _postgres_module()
    calls: dict[str, object] = {}
    monkeypatch.setenv("PGAPPNAME", "gobby-tests")
    monkeypatch.setenv("PGPOOL_MIN", "99")
    monkeypatch.setenv("PGPOOL_MAX", "100")
    monkeypatch.setenv("PGPOOL_TIMEOUT", "101")
    monkeypatch.setenv("PGPOOL_OPEN_TIMEOUT", "102")

    class FakePool:
        def __init__(self, *args, **kwargs) -> None:
            calls["constructor_open"] = kwargs["open"]
            calls["constructor_min_size"] = kwargs["min_size"]
            calls["constructor_max_size"] = kwargs["max_size"]
            calls["constructor_timeout"] = kwargs["timeout"]
            calls["pool_kwargs"] = kwargs["kwargs"]

        def open(self, *, wait: bool, timeout: float) -> None:
            calls["opened"] = (wait, timeout)

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(module, "ConnectionPool", FakePool)

    db = module.PostgresHubDatabase(
        "postgresql://gobby:secret@localhost/gobby",
        pool_config=PostgresPoolConfig(
            min_size=4,
            max_size=24,
            acquire_timeout_seconds=7.5,
            open_timeout_seconds=12.5,
        ),
    )
    db.open()

    assert calls["constructor_open"] is False
    assert calls["constructor_min_size"] == 4
    assert calls["constructor_max_size"] == 24
    assert calls["constructor_timeout"] == 7.5
    assert calls["pool_kwargs"]["application_name"].startswith("gobby-hub-")
    assert calls["pool_kwargs"]["connect_timeout"] == 10
    assert calls["pool_kwargs"]["keepalives"] == 1
    assert calls["pool_kwargs"]["keepalives_idle"] == 30
    assert calls["pool_kwargs"]["keepalives_interval"] == 10
    assert calls["pool_kwargs"]["keepalives_count"] == 3
    assert calls["opened"] == (True, 12.5)


def test_transaction_pool_timeout_checks_pool_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _postgres_module()
    pool_holder: dict[str, object] = {}

    class TransactionContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class Connection:
        def transaction(self):
            return TransactionContext()

    class TimeoutConnectionContext:
        def __enter__(self):
            raise module._postgres_pool.PoolTimeout("couldn't get a connection after 5.00 sec")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class ConnectionContext:
        def __enter__(self):
            return Connection()

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class FakePool:
        def __init__(self, *args, **kwargs) -> None:
            self.connection_calls = 0
            self.check_calls = 0
            pool_holder["pool"] = self

        def open(self, *, wait: bool, timeout: float) -> None:
            return None

        def connection(self):
            self.connection_calls += 1
            if self.connection_calls == 1:
                return TimeoutConnectionContext()
            return ConnectionContext()

        def check(self) -> None:
            self.check_calls += 1

        def get_stats(self) -> dict[str, int]:
            return {"pool_size": 20, "pool_available": 0}

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "ConnectionPool", FakePool)
    db = module.PostgresHubDatabase("postgresql://gobby:secret@localhost/gobby")

    with caplog.at_level(logging.WARNING, logger=module.__name__):
        with db.transaction():
            pass

    pool = pool_holder["pool"]
    assert pool.connection_calls == 2
    assert pool.check_calls == 1
    assert "retrying once" in caplog.text
    assert "pool_size" in caplog.text


def test_transaction_pool_timeout_retry_failure_logs_stats_and_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _postgres_module()
    pool_holder: dict[str, object] = {}

    class TimeoutConnectionContext:
        def __enter__(self):
            raise module._postgres_pool.PoolTimeout("couldn't get a connection after 5.00 sec")

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class FakePool:
        def __init__(self, *args, **kwargs) -> None:
            self.connection_calls = 0
            self.check_calls = 0
            pool_holder["pool"] = self

        def open(self, *, wait: bool, timeout: float) -> None:
            return None

        def connection(self):
            self.connection_calls += 1
            return TimeoutConnectionContext()

        def check(self) -> None:
            self.check_calls += 1

        def get_stats(self) -> dict[str, int]:
            return {"pool_size": 20, "pool_available": 0}

        def close(self) -> None:
            return None

    monkeypatch.setattr(module, "ConnectionPool", FakePool)
    db = module.PostgresHubDatabase("postgresql://gobby:secret@localhost/gobby")

    with caplog.at_level(logging.WARNING, logger=module.__name__):
        with pytest.raises(module._postgres_pool.PoolTimeout):
            with db.transaction():
                pass

    pool = pool_holder["pool"]
    assert pool.connection_calls == 2
    assert pool.check_calls == 1
    assert "retry failed" in caplog.text
    assert "pool_size" in caplog.text


def test_bounded_transaction_sets_local_bounds_before_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()
    db = object.__new__(module.PostgresHubDatabase)
    statements: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        def fetchone(self) -> dict[str, str]:
            return {
                "statement_timeout": "0",
                "lock_timeout": "0",
            }

    class Transaction:
        def execute(
            self,
            sql: str,
            params: tuple[object, ...] = (),
        ) -> Cursor:
            statements.append((sql, params))
            return Cursor()

    transaction = Transaction()

    @contextmanager
    def fake_transaction(_self):
        yield transaction

    monkeypatch.setattr(module.PostgresHubDatabase, "transaction", fake_transaction)

    with db.bounded_transaction(statement_timeout_ms=1234, lock_timeout_ms=567) as bounded:
        bounded.execute("SELECT 1")

    assert statements == [
        (
            "SELECT current_setting('statement_timeout') AS statement_timeout, "
            "current_setting('lock_timeout') AS lock_timeout",
            (),
        ),
        ("SELECT set_config('statement_timeout', %s, true)", ("1234ms",)),
        ("SELECT set_config('lock_timeout', %s, true)", ("567ms",)),
        ("SELECT 1", ()),
        ("SELECT set_config('statement_timeout', %s, true)", ("0",)),
        ("SELECT set_config('lock_timeout', %s, true)", ("0",)),
    ]


@pytest.mark.parametrize(
    ("statement_timeout_ms", "lock_timeout_ms"),
    [(0, 1), (-1, 1), (1, 0), (1, -1)],
)
def test_bounded_transaction_rejects_nonpositive_bounds(
    statement_timeout_ms: int,
    lock_timeout_ms: int,
) -> None:
    module = _postgres_module()
    db = object.__new__(module.PostgresHubDatabase)

    with pytest.raises(ValueError, match="positive milliseconds"):
        with db.bounded_transaction(
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
        ):
            pass


def test_bounded_transaction_restores_outer_bounds_after_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()
    db = object.__new__(module.PostgresHubDatabase)
    settings = {
        "statement_timeout": "2500ms",
        "lock_timeout": "750ms",
    }

    class Cursor:
        def fetchone(self) -> dict[str, str]:
            return dict(settings)

    class Transaction:
        def execute(
            self,
            sql: str,
            params: tuple[object, ...] = (),
        ) -> Cursor:
            if "set_config('statement_timeout'" in sql:
                settings["statement_timeout"] = str(params[0])
            elif "set_config('lock_timeout'" in sql:
                settings["lock_timeout"] = str(params[0])
            return Cursor()

    @contextmanager
    def fake_transaction(_self):
        yield Transaction()

    monkeypatch.setattr(module.PostgresHubDatabase, "transaction", fake_transaction)

    with pytest.raises(RuntimeError, match="body failed"):
        with db.bounded_transaction(statement_timeout_ms=100, lock_timeout_ms=50):
            raise RuntimeError("body failed")

    assert settings == {
        "statement_timeout": "2500ms",
        "lock_timeout": "750ms",
    }


def test_bounded_transaction_restores_nested_transaction_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()
    db = object.__new__(module.PostgresHubDatabase)
    settings = {
        "statement_timeout": "2500ms",
        "lock_timeout": "750ms",
    }

    class Cursor:
        def fetchone(self) -> dict[str, str]:
            return dict(settings)

    class Transaction:
        def execute(
            self,
            sql: str,
            params: tuple[object, ...] = (),
        ) -> Cursor:
            if "set_config('statement_timeout'" in sql:
                settings["statement_timeout"] = str(params[0])
            elif "set_config('lock_timeout'" in sql:
                settings["lock_timeout"] = str(params[0])
            return Cursor()

    @contextmanager
    def fake_transaction(_self):
        yield Transaction()

    monkeypatch.setattr(module.PostgresHubDatabase, "transaction", fake_transaction)

    with db.bounded_transaction(statement_timeout_ms=100, lock_timeout_ms=50):
        assert settings == {
            "statement_timeout": "100ms",
            "lock_timeout": "50ms",
        }
        with db.bounded_transaction(statement_timeout_ms=20, lock_timeout_ms=10):
            assert settings == {
                "statement_timeout": "20ms",
                "lock_timeout": "10ms",
            }
        assert settings == {
            "statement_timeout": "100ms",
            "lock_timeout": "50ms",
        }

    assert settings == {
        "statement_timeout": "2500ms",
        "lock_timeout": "750ms",
    }


def test_postgres_close_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _postgres_module()
    calls: dict[str, int] = {"close": 0}
    close_timeouts: list[float] = []

    class FakePool:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open(self, *, wait: bool, timeout: float) -> None:
            raise AssertionError("open should not be called by close")

        def close(self, *, timeout: float) -> None:
            calls["close"] += 1
            close_timeouts.append(timeout)

    monkeypatch.setattr(module, "ConnectionPool", FakePool)

    db = module.PostgresHubDatabase("postgresql://gobby:secret@localhost/gobby")
    db.close()
    db.close()

    assert calls["close"] == 1
    assert close_timeouts == [module._POOL_CLOSE_TIMEOUT_SECONDS]


def test_postgres_open_after_close_raises_without_reopening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()
    calls: dict[str, int] = {"open": 0, "close": 0}
    close_timeouts: list[float] = []

    class FakePool:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def open(self, *, wait: bool, timeout: float) -> None:
            calls["open"] += 1

        def close(self, *, timeout: float) -> None:
            calls["close"] += 1
            close_timeouts.append(timeout)

    monkeypatch.setattr(module, "ConnectionPool", FakePool)

    db = module.PostgresHubDatabase("postgresql://gobby:secret@localhost/gobby")
    db.close()

    with pytest.raises(RuntimeError, match="connection pool is closed"):
        db.open()
    with pytest.raises(RuntimeError, match="connection pool is closed"):
        with db.transaction():
            pass

    assert calls == {"open": 0, "close": 1}
    assert close_timeouts == [module._POOL_CLOSE_TIMEOUT_SECONDS]


def test_apply_postgres_baseline_uses_transaction_scoped_advisory_lock(monkeypatch) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("fresh")
    locked = _ApplyConnection("fresh")
    resources = _Resources()

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", resources)
    db = _new_db(module, _Pool(fast, locked))

    db._apply_postgres_baseline()

    assert locked.transaction_entered is True
    assert locked.transaction_exited is True
    assert any("pg_advisory_xact_lock" in statement for statement in locked.statements)
    assert any(
        "pg_extension" in statement and "pg_search" in statement for statement in locked.statements
    )
    assert not any("pg_advisory_lock(" in statement for statement in locked.statements)
    assert "CREATE TABLE tasks(id INTEGER)" in locked.statements
    assert any("INSERT INTO schema_migrations" in statement for statement in locked.statements)
    assert resources.read_count == 1


def test_apply_postgres_baseline_reclassifies_under_lock_and_skips_racing_apply(
    monkeypatch,
) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("fresh")
    locked = _ApplyConnection("already_baselined")

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", _Resources())
    db = _new_db(module, _Pool(fast, locked))

    db._apply_postgres_baseline()

    assert any("pg_advisory_xact_lock" in statement for statement in locked.statements)
    assert not any(
        "pg_extension" in statement and "pg_search" in statement for statement in locked.statements
    )
    assert "CREATE TABLE tasks(id INTEGER)" not in locked.statements
    assert not any("INSERT INTO schema_migrations" in statement for statement in locked.statements)


def test_apply_postgres_baseline_rejects_partial_baseline_state(monkeypatch) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("corrupt_partial")
    locked = _ApplyConnection("corrupt_partial")

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", _Resources())
    db = _new_db(module, _Pool(fast, locked))

    with pytest.raises(MigrationUnsupportedError, match="Unrecognized PostgreSQL schema"):
        db._apply_postgres_baseline()

    assert "CREATE TABLE tasks(id INTEGER)" not in locked.statements


def test_apply_postgres_baseline_rejects_pre_baseline_lineage_with_observed_state(
    monkeypatch,
) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("corrupt_partial")
    locked = _ApplyConnection(
        "corrupt_partial",
        max_version=BASELINE_VERSION - 1,
        tables={"schema_migrations", "tasks", "sessions"},
    )

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    db = _new_db(module, _Pool(fast, locked))

    with pytest.raises(MigrationUnsupportedError) as exc_info:
        db._apply_postgres_baseline()

    message = str(exc_info.value)
    assert f"pre-{BASELINE_VERSION}" in message
    assert f"max schema version {BASELINE_VERSION - 1}" in message
    assert "tables ['sessions', 'tasks']" in message
    assert "Post-baseline repair migrations do not run for this lineage" in message


def test_apply_postgres_baseline_adopts_gcore_code_index_state(monkeypatch) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("gcore_code_index")
    locked = _ApplyConnection("gcore_code_index")
    resources = _GcoreAdoptionResources()

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", resources)
    db = _new_db(module, _Pool(fast, locked))

    db._apply_postgres_baseline()

    stripped = [statement.strip() for statement in locked.statements]
    assert "CREATE TABLE tasks(id INTEGER)" in stripped
    assert "CREATE TABLE code_symbols(id TEXT PRIMARY KEY)" not in stripped
    assert "CREATE INDEX IF NOT EXISTS idx_cs_project ON code_symbols(project_id)" in stripped
    assert "CREATE TABLE gwiki_documents(id TEXT PRIMARY KEY)" not in stripped
    assert "CREATE TABLE gwiki_chunks(id TEXT PRIMARY KEY, document_id TEXT)" not in stripped
    assert (
        "CREATE INDEX IF NOT EXISTS idx_gwiki_chunks_document ON gwiki_chunks(document_id)"
        in stripped
    )
    assert "CREATE TABLE gwiki_sources(id TEXT PRIMARY KEY)" not in stripped
    assert any(
        statement.lstrip().startswith("CREATE INDEX IF NOT EXISTS code_symbols_search_bm25")
        for statement in locked.statements
    )
    assert any("INSERT INTO schema_migrations" in statement for statement in locked.statements)
    assert resources.read_count == 1


def test_apply_postgres_baseline_adopts_gwiki_standalone_state(monkeypatch) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("gwiki_standalone")
    locked = _ApplyConnection("gwiki_standalone")
    resources = _GwikiAdoptionResources()

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", resources)
    db = _new_db(module, _Pool(fast, locked))

    db._apply_postgres_baseline()

    stripped = [statement.strip() for statement in locked.statements]
    assert "CREATE TABLE gwiki_documents(id TEXT PRIMARY KEY)" not in stripped
    assert "CREATE TABLE gwiki_chunks(id TEXT PRIMARY KEY, document_id TEXT)" not in stripped
    assert (
        "CREATE INDEX IF NOT EXISTS idx_gwiki_chunks_document ON gwiki_chunks(document_id)"
        in stripped
    )
    assert "CREATE TABLE gwiki_sources(id TEXT PRIMARY KEY)" not in stripped
    assert "CREATE TABLE tasks(id INTEGER)" in stripped
    assert any("INSERT INTO schema_migrations" in statement for statement in locked.statements)
    assert resources.read_count == 1


def test_apply_postgres_baseline_rejects_adopted_schema_with_missing_columns(
    monkeypatch,
) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("gcore_code_index")
    contracts = {**module._GCORE_CODE_INDEX_COLUMNS, **module._GWIKI_COLUMNS}
    columns = {table: set(expected) for table, expected in contracts.items()}
    columns["code_symbols"].remove("summary")
    locked = _ApplyConnection("gcore_code_index", columns=columns)

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", _GcoreAdoptionResources())
    db = _new_db(module, _Pool(fast, locked))

    with pytest.raises(
        MigrationUnsupportedError,
        match=r"code_symbols: summary",
    ):
        db._apply_postgres_baseline()

    assert not any("INSERT INTO schema_migrations" in statement for statement in locked.statements)


def test_apply_postgres_baseline_rejects_missing_pg_search_without_extension_ddl(
    monkeypatch,
) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("fresh")
    locked = _ApplyConnection("fresh", pg_search_present=False)
    resources = _Resources()

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", resources)
    db = _new_db(module, _Pool(fast, locked))

    with pytest.raises(MigrationUnsupportedError) as exc_info:
        db._apply_postgres_baseline()

    assert str(exc_info.value) == module._PG_SEARCH_MISSING_MESSAGE
    upper_statements = [statement.upper() for statement in locked.statements]
    assert any(
        "PG_EXTENSION" in statement and "PG_SEARCH" in statement for statement in upper_statements
    )
    assert all("CREATE EXTENSION" not in statement for statement in upper_statements)
    assert "CREATE TABLE tasks(id INTEGER)" not in locked.statements
    assert resources.read_count == 0


def test_apply_postgres_baseline_does_not_require_pgcrypto_extension(
    monkeypatch,
) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("fresh")
    locked = _ApplyConnection("fresh", pgcrypto_present=False)
    resources = _Resources()

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", resources)
    db = _new_db(module, _Pool(fast, locked))

    db._apply_postgres_baseline()

    upper_statements = [statement.upper() for statement in locked.statements]
    assert not any("PGCRYPTO" in statement for statement in upper_statements)
    assert all("CREATE EXTENSION" not in statement for statement in upper_statements)
    assert "CREATE TABLE tasks(id INTEGER)" in locked.statements
    assert resources.read_count == 1


def test_apply_migrations_proceeds_when_pg_search_present(monkeypatch) -> None:
    module = _postgres_module()
    calls: list[str] = []
    initial = _ApplyConnection("fresh")
    fast = _ApplyConnection("fresh")
    locked = _ApplyConnection("fresh")

    class FakeRunner:
        def __init__(
            self,
            hub: HubDatabase,
            *,
            autocommit_connection: Callable[[], Any],
        ) -> None:
            self.hub = hub
            self.autocommit_connection = autocommit_connection
            created_runners.append(self)

        def apply_pending(self, *, fresh_schema: bool = False) -> None:
            calls.append(f"file_migrations:{fresh_schema}")

    created_runners: list[FakeRunner] = []
    monkeypatch.setattr(module, "MigrationRunner", FakeRunner)
    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", _Resources())
    db = _new_db(module, _Pool(initial, fast, locked))

    db.apply_migrations()

    assert calls == ["file_migrations:True"]
    assert len(created_runners) == 1
    assert created_runners[0].autocommit_connection == db._open_advisory_lock_connection
    assert "CREATE TABLE tasks(id INTEGER)" in locked.statements
    assert all("CREATE EXTENSION" not in statement.upper() for statement in locked.statements)


def test_apply_postgres_baseline_probe_only_when_required_extensions_preinstalled(
    monkeypatch,
) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("fresh")
    locked = _ApplyConnection("fresh", pg_search_present=True)

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", _Resources())
    db = _new_db(module, _Pool(fast, locked))

    db._apply_postgres_baseline()

    probe_statements = [statement for statement in locked.statements if "pg_extension" in statement]
    assert probe_statements == [
        "SELECT 1 FROM pg_extension WHERE extname = %s ('pg_search',)",
    ]
    assert all("CREATE EXTENSION" not in statement.upper() for statement in locked.statements)


def test_apply_migrations_runs_postgres_baseline_before_file_migrations(monkeypatch) -> None:
    module = _postgres_module()
    calls: list[str] = []

    class FakeRunner:
        def __init__(self, hub: Any, *, autocommit_connection: Any) -> None:
            self.hub = hub
            self.autocommit_connection = autocommit_connection

        def apply_pending(self, *, fresh_schema: bool = False) -> None:
            calls.append(f"file_migrations:{fresh_schema}")

    monkeypatch.setattr(module, "MigrationRunner", FakeRunner)
    monkeypatch.setattr(
        module.PostgresHubDatabase,
        "_postgres_baseline_already_applied",
        lambda self: False,
    )
    monkeypatch.setattr(
        module.PostgresHubDatabase,
        "_apply_postgres_baseline",
        lambda self: calls.append("postgres_baseline"),
    )

    db = object.__new__(module.PostgresHubDatabase)
    db.apply_migrations()

    assert calls == ["postgres_baseline", "file_migrations:True"]


def test_apply_migrations_skips_postgres_baseline_when_already_applied(monkeypatch) -> None:
    module = _postgres_module()
    calls: list[str] = []

    class FakeRunner:
        def __init__(self, hub: Any, *, autocommit_connection: Any) -> None:
            self.hub = hub
            self.autocommit_connection = autocommit_connection

        def apply_pending(self, *, fresh_schema: bool = False) -> None:
            calls.append(f"file_migrations:{fresh_schema}")

    monkeypatch.setattr(module, "MigrationRunner", FakeRunner)
    monkeypatch.setattr(
        module.PostgresHubDatabase,
        "_postgres_baseline_already_applied",
        lambda self: True,
    )
    monkeypatch.setattr(
        module.PostgresHubDatabase,
        "_apply_postgres_baseline",
        lambda self: calls.append("postgres_baseline"),
    )

    db = object.__new__(module.PostgresHubDatabase)
    db.apply_migrations()

    assert calls == ["file_migrations:False"]
