"""Shared table filters for SQLite-to-PostgreSQL migration."""

from __future__ import annotations

import sqlite3
from typing import Any

SQLITE_BOOKKEEPING_TABLES: frozenset[str] = frozenset({"schema_version", "schema_migrations"})
SQLITE_FTS_TABLES: frozenset[str] = frozenset(
    {
        "tasks_fts",
        "memories_fts",
        "code_symbols_fts",
        "code_content_fts",
        "skills_fts",
    }
)

# These tables are legacy or runtime-local state rather than durable hub data.
# They are intentionally not copied during the one-shot hub migration.
IGNORED_MIGRATION_TABLES: frozenset[str] = frozenset(
    {
        "auth_challenges",
        "auth_sessions",
        "passkey_credentials",
        "pending_approvals",
        "session_message_state",
    }
)


def sqlite_application_tables(source: sqlite3.Connection) -> set[str]:
    rows = source.execute(
        """
        SELECT name, sql
          FROM sqlite_master
         WHERE type = 'table'
           AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    tables: set[str] = set()
    for row in rows:
        name = str(row_value(row, "name"))
        ddl = str(row_value(row, "sql") or "")
        if (
            name in SQLITE_BOOKKEEPING_TABLES
            or name in IGNORED_MIGRATION_TABLES
            or is_sqlite_fts_table(name, ddl)
        ):
            continue
        tables.add(name)
    return tables


def is_sqlite_fts_table(name: str, ddl: str) -> bool:
    if name in SQLITE_FTS_TABLES:
        return True
    if any(name.startswith(f"{fts_table}_") for fts_table in SQLITE_FTS_TABLES):
        return True
    return "USING fts5" in ddl


def row_value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]
