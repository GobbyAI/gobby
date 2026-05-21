from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import MigrationUnsupportedError

pytestmark = pytest.mark.unit


def _migration_module():
    return importlib.import_module("gobby.storage.migrations")


def _split(sql: str) -> list[str]:
    module = _migration_module()
    return [
        statement.strip()
        for statement in module._split_statements_respecting_dollar_quotes(sql)
        if statement.strip()
    ]


def _table_exists(db: LocalDatabase, table: str) -> bool:
    row = db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return row is not None


def _create_bookkeeping_table(db: LocalDatabase, table: str, versions: list[int]) -> None:
    db.execute(
        f"""
        CREATE TABLE {table} (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for version in versions:
        db.execute(f"INSERT INTO {table} (version) VALUES (?)", (version,))


def _versions(db: LocalDatabase, table: str) -> list[int]:
    return [row["version"] for row in db.fetchall(f"SELECT version FROM {table} ORDER BY version")]


class _Result:
    def __init__(self, rows=()) -> None:
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _PostgresBookkeepingHub:
    dialect = "postgres"

    def __init__(self) -> None:
        self.tables = {"schema_migrations"}
        self.statements: list[str] = []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, sql: str, params=()):
        self.statements.append(sql)
        if "to_regclass" in sql:
            return _Result([{"table_exists": params[0] in self.tables}])
        raise AssertionError(f"unexpected query: {sql}")


def test_split_statements_respecting_dollar_quotes_keeps_function_bodies_intact() -> None:
    statements = _split(
        """
        CREATE FUNCTION f() RETURNS void AS $$
        BEGIN
            PERFORM 1;
            PERFORM 2;
        END;
        $$ LANGUAGE plpgsql;
        SELECT 1;
        """
    )

    assert len(statements) == 2
    assert "PERFORM 1;\n            PERFORM 2;" in statements[0]
    assert statements[1] == "SELECT 1"


def test_split_statements_respecting_dollar_quotes_handles_nested_and_adjacent_tags() -> None:
    statements = _split(
        """
        SELECT $outer$begin $inner$still; text$inner$ end;$outer$;
        SELECT $tag1$a;b$tag1$ || $tag2$c;d$tag2$;
        SELECT $$empty-tag; body$$;
        """
    )

    assert len(statements) == 3
    assert "$inner$still; text$inner$" in statements[0]
    assert "$tag1$a;b$tag1$ || $tag2$c;d$tag2$" in statements[1]
    assert "$$empty-tag; body$$" in statements[2]


def test_split_statements_respecting_dollar_quotes_ignores_strings_and_comments() -> None:
    statements = _split(
        """
        SELECT 'can''t split; here';
        SELECT "odd;identifier";
        -- comment with a semicolon; and $tag$
        SELECT 1;
        /* block comment; with $1 */
        SELECT '$$not a tag;$$';
        """
    )

    assert len(statements) == 4
    assert statements[0] == "SELECT 'can''t split; here'"
    assert statements[1] == 'SELECT "odd;identifier"'
    assert "SELECT 1" in statements[2]
    assert "SELECT '$$not a tag;$$'" in statements[3]


def test_split_statements_respecting_dollar_quotes_ignores_mixed_contexts_inside_body() -> None:
    statements = _split(
        """
        CREATE FUNCTION noisy() RETURNS void AS $body$
        BEGIN
            RAISE NOTICE 'string; still body';
            -- comment; still body
            /* block; still body */
            PERFORM $$inner dollar; still body$$;
        END;
        $body$ LANGUAGE plpgsql;
        SELECT 'outside; string';
        """
    )

    assert len(statements) == 2
    assert "RAISE NOTICE 'string; still body';" in statements[0]
    assert "PERFORM $$inner dollar; still body$$;" in statements[0]
    assert statements[1] == "SELECT 'outside; string'"


def test_split_statements_respecting_dollar_quotes_handles_checked_in_postgres_ddl() -> None:
    sql = (
        Path(__file__).resolve().parents[2] / "src/gobby/storage/postgres_baseline_schema.sql"
    ).read_text()
    statements = _split(sql)

    function_statements = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("CREATE FUNCTION")
    ]

    assert function_statements
    assert any(
        "enforce_chat_attachments_bound_at_write_once" in statement
        and "RAISE EXCEPTION 'chat_attachments.bound_at is write-once';" in statement
        for statement in function_statements
    )
    assert any(
        "refresh_task_state_bucket_from_stage" in statement
        and "IF TG_OP = 'DELETE' THEN" in statement
        for statement in function_statements
    )


def test_bookkeeping_table_rename_paths(tmp_path) -> None:
    module = _migration_module()

    old_only = LocalDatabase(tmp_path / "old-only.db")
    _create_bookkeeping_table(old_only, "schema_version", [240, 241, 242, 244])
    module._migrate_bookkeeping_table(old_only)

    assert not _table_exists(old_only, "schema_version")
    assert _table_exists(old_only, "schema_migrations")
    assert _versions(old_only, "schema_migrations") == [240, 241, 242, 244]

    module._migrate_bookkeeping_table(old_only)
    assert _versions(old_only, "schema_migrations") == [240, 241, 242, 244]
    old_only.close()

    identical = LocalDatabase(tmp_path / "identical.db")
    _create_bookkeeping_table(identical, "schema_version", [244])
    _create_bookkeeping_table(identical, "schema_migrations", [244])
    module._migrate_bookkeeping_table(identical)

    assert not _table_exists(identical, "schema_version")
    assert _table_exists(identical, "schema_migrations")
    assert _versions(identical, "schema_migrations") == [244]
    identical.close()

    divergent = LocalDatabase(tmp_path / "divergent.db")
    _create_bookkeeping_table(divergent, "schema_version", [244])
    _create_bookkeeping_table(divergent, "schema_migrations", [245])

    with pytest.raises(MigrationUnsupportedError, match="divergent") as exc_info:
        module._migrate_bookkeeping_table(divergent)

    message = str(exc_info.value)
    assert "PostgreSQL hub database" in message
    assert "known-good backup" in message
    assert "gobby-hub.db" not in message

    divergent.close()


def test_bookkeeping_table_is_noop_for_postgres_schema_migrations_only() -> None:
    module = _migration_module()
    hub = _PostgresBookkeepingHub()

    module._migrate_bookkeeping_table(hub)

    assert not any("ALTER TABLE" in statement for statement in hub.statements)
    assert not any("DROP TABLE" in statement for statement in hub.statements)


def test_migration_runner_rejects_sqlite_hubs_after_cutover(tmp_path) -> None:
    from gobby.storage.database import LocalDatabase

    migration_module = _migration_module()
    db = LocalDatabase(tmp_path / "brand-new.db")

    try:
        with pytest.raises(MigrationUnsupportedError, match="SQLite hub migrations were removed"):
            migration_module.MigrationRunner(db).apply_pending()
    finally:
        db.close()
