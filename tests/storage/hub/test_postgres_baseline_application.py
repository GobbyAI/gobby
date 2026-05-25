from __future__ import annotations

import importlib
from contextlib import contextmanager

import pytest

from gobby.storage.migrations import BASELINE_VERSION, MigrationUnsupportedError

pytestmark = pytest.mark.unit


def _postgres_module():
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
        if "schema_migrations" in sql:
            version = BASELINE_VERSION if not params else params[0]
            return _Result([(1,)] if version in self.baseline_versions else [])
        raise AssertionError(f"unexpected query: {sql}")


@pytest.mark.parametrize(
    ("tables", "versions", "expected"),
    [
        (set(), set(), "fresh"),
        ({"gobby_install_ownership"}, set(), "fresh_with_install_infra"),
        ({"_pgaudit_probe"}, set(), "fresh_with_install_infra"),
        ({"schema_migrations", "tasks"}, {BASELINE_VERSION}, "already_baselined"),
        ({"schema_migrations"}, set(), "fresh"),
        ({"tasks"}, set(), "corrupt_partial"),
        ({"gobby_install_ownership", "tasks"}, set(), "corrupt_partial"),
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
    def __init__(self, state: str, *, pg_search_present: bool = True) -> None:
        self.state = state
        self.pg_search_present = pg_search_present
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
        self.statements.append(sql)
        if "pg_extension" in sql and "pg_search" in sql:
            return _Result([(1,)] if self.pg_search_present else [])
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


def _new_db(module, pool: _Pool):
    db = object.__new__(module.PostgresHubDatabase)
    db._pool = pool
    return db


def test_postgres_pool_opens_lazily(monkeypatch) -> None:
    module = _postgres_module()
    calls: dict[str, object] = {}

    class FakePool:
        def __init__(self, *args, **kwargs) -> None:
            calls["constructor_open"] = kwargs["open"]

        def open(self, *, wait: bool, timeout: float) -> None:
            calls["opened"] = (wait, timeout)

        def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(module, "ConnectionPool", FakePool)

    db = module.PostgresHubDatabase("postgresql://gobby:secret@localhost/gobby")
    assert calls["constructor_open"] is False
    assert "opened" not in calls

    db.open(timeout=1.5)
    db.open(timeout=9.0)

    assert calls["opened"] == (True, 1.5)


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

    with pytest.raises(MigrationUnsupportedError, match="dump-and-restore"):
        db._apply_postgres_baseline()

    assert "CREATE TABLE tasks(id INTEGER)" not in locked.statements


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


def test_apply_migrations_proceeds_when_pg_search_present(monkeypatch) -> None:
    module = _postgres_module()
    calls: list[str] = []
    initial = _ApplyConnection("fresh")
    fast = _ApplyConnection("fresh")
    locked = _ApplyConnection("fresh")

    class FakeRunner:
        def __init__(self, hub) -> None:
            self.hub = hub

        def apply_pending(self) -> None:
            calls.append("file_migrations")

    monkeypatch.setattr(module, "MigrationRunner", FakeRunner)
    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", _Resources())
    db = _new_db(module, _Pool(initial, fast, locked))

    db.apply_migrations()

    assert calls == ["file_migrations"]
    assert "CREATE TABLE tasks(id INTEGER)" in locked.statements
    assert all("CREATE EXTENSION" not in statement.upper() for statement in locked.statements)


def test_apply_postgres_baseline_probe_only_when_pg_search_preinstalled(monkeypatch) -> None:
    module = _postgres_module()
    fast = _ApplyConnection("fresh")
    locked = _ApplyConnection("fresh", pg_search_present=True)

    monkeypatch.setattr(module, "_classify_baseline_state", lambda conn: conn.state)
    monkeypatch.setattr(module.importlib, "resources", _Resources())
    db = _new_db(module, _Pool(fast, locked))

    db._apply_postgres_baseline()

    probe_statements = [
        statement
        for statement in locked.statements
        if "pg_extension" in statement and "pg_search" in statement
    ]
    assert probe_statements == ["SELECT 1 FROM pg_extension WHERE extname = 'pg_search'"]
    assert all("CREATE EXTENSION" not in statement.upper() for statement in locked.statements)


def test_apply_migrations_runs_postgres_baseline_before_file_migrations(monkeypatch) -> None:
    module = _postgres_module()
    calls: list[str] = []

    class FakeRunner:
        def __init__(self, hub) -> None:
            self.hub = hub

        def apply_pending(self) -> None:
            calls.append("file_migrations")

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

    assert calls == ["postgres_baseline", "file_migrations"]


def test_apply_migrations_skips_postgres_baseline_when_already_applied(monkeypatch) -> None:
    module = _postgres_module()
    calls: list[str] = []

    class FakeRunner:
        def __init__(self, hub) -> None:
            self.hub = hub

        def apply_pending(self) -> None:
            calls.append("file_migrations")

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

    assert calls == ["file_migrations"]
