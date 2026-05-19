from __future__ import annotations

import importlib

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
        -- comment with a semicolon; and $tag$
        SELECT 1;
        /* block comment; with $1 */
        SELECT '$$not a tag;$$';
        """
    )

    assert len(statements) == 3
    assert statements[0] == "SELECT 'can''t split; here'"
    assert "SELECT 1" in statements[1]
    assert "SELECT '$$not a tag;$$'" in statements[2]


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

    with pytest.raises(MigrationUnsupportedError, match="divergent"):
        module._migrate_bookkeeping_table(divergent)

    divergent.close()


def test_migration_runner_creates_schema_migrations_for_brand_new_sqlite(tmp_path) -> None:
    sqlite_module = importlib.import_module("gobby.storage.hub.sqlite")
    migration_module = _migration_module()
    db = sqlite_module.SqliteHubDatabase(str(tmp_path / "brand-new.db"))

    try:
        migration_module.MigrationRunner(db).apply_pending()
        with db.transaction() as tx:
            row = tx.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = $1",
                ("schema_migrations",),
            ).fetchone()
    finally:
        db.close()

    assert row is not None
