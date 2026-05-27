from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path
from types import MethodType

import pytest

from gobby.storage.migrations import Migration, MigrationUnsupportedError

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


class _Result:
    def __init__(self, rows=()) -> None:
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _PostgresMigrationHub:
    dialect = "postgres"

    def __init__(self) -> None:
        self.tables = {"schema_migrations"}
        self.applied: list[int] = []

    @contextmanager
    def transaction(self):
        yield self

    def execute(self, sql: str, params=()):
        if "to_regclass" in sql:
            return _Result([{"table_exists": params[0] in self.tables}])
        raise AssertionError(f"unexpected query: {sql}")


def test_postgres_pending_migration_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    module = _migration_module()
    hub = _PostgresMigrationHub()
    runner = module.MigrationRunner(hub)
    migration = Migration(
        version=262,
        name="add_needed_column",
        path=Path("unused.sql"),
    )

    def ensure_schema_migrations_table(self) -> None:
        return None

    def read_applied_versions(self) -> set[int]:
        return set()

    def discover_migrations(self) -> list[Migration]:
        return [migration]

    def run_migration(self, txn, discovered: Migration) -> None:
        assert txn is hub
        assert discovered is migration

    def record_applied_version(self, txn, version: int) -> None:
        assert txn is hub
        hub.applied.append(version)

    runner._ensure_schema_migrations_table = MethodType(ensure_schema_migrations_table, runner)
    runner._read_applied_versions = MethodType(read_applied_versions, runner)
    runner._discover_migrations = MethodType(discover_migrations, runner)
    runner._run_migration = MethodType(run_migration, runner)
    runner._record_applied_version = MethodType(record_applied_version, runner)

    with caplog.at_level("WARNING", logger="gobby.storage.migrations"):
        runner.apply_pending()

    assert hub.applied == [262]
    assert "Applying PostgreSQL migration 262_add_needed_column" in caplog.text


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
        /* block comment; with %s */
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


def test_migration_runner_rejects_non_postgres_hubs_after_cutover() -> None:
    class LegacyHub:
        dialect = "legacy"

    migration_module = _migration_module()

    with pytest.raises(MigrationUnsupportedError, match="only supports PostgreSQL"):
        migration_module.MigrationRunner(LegacyHub())
