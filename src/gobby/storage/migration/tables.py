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
SQLITE_IMPORT_ROW_FILTERS: dict[str, str] = {
    "memory_crossrefs": (
        "EXISTS (SELECT 1 FROM memories AS parent WHERE parent.id = memory_crossrefs.source_id) "
        "AND EXISTS (SELECT 1 FROM memories AS parent WHERE parent.id = memory_crossrefs.target_id)"
    ),
    "session_skills": (
        "EXISTS (SELECT 1 FROM sessions AS parent WHERE parent.id = session_skills.session_id)"
    ),
    "session_tasks": (
        "EXISTS (SELECT 1 FROM sessions AS parent WHERE parent.id = session_tasks.session_id) "
        "AND EXISTS (SELECT 1 FROM tasks AS parent WHERE parent.id = session_tasks.task_id)"
    ),
    "step_executions": (
        "EXISTS ("
        "SELECT 1 FROM pipeline_executions AS parent "
        "WHERE parent.id = step_executions.execution_id"
        ")"
    ),
    "task_dependencies": (
        "EXISTS (SELECT 1 FROM tasks AS parent WHERE parent.id = task_dependencies.task_id) "
        "AND EXISTS (SELECT 1 FROM tasks AS parent WHERE parent.id = task_dependencies.depends_on)"
    ),
    "tools": "EXISTS (SELECT 1 FROM mcp_servers AS parent WHERE parent.id = tools.mcp_server_id)",
    "workflow_states": (
        "EXISTS (SELECT 1 FROM sessions AS parent WHERE parent.id = workflow_states.session_id)"
    ),
}
NULL_ORPHAN_REFERENCES: dict[str, dict[str, str]] = {
    "sessions": {
        "agent_run_id": "agent_runs",
        "parent_session_id": "sessions",
    },
    "tasks": {
        "claimed_by_session_id": "sessions",
        "closed_in_session_id": "sessions",
        "created_in_session_id": "sessions",
        "parent_task_id": "tasks",
    },
}


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


def sqlite_import_where_clause(table: str) -> str:
    return SQLITE_IMPORT_ROW_FILTERS.get(table, "1=1")


def repair_orphan_reference_value(
    source: sqlite3.Connection,
    *,
    table: str,
    column: str,
    value: Any,
    cache: dict[tuple[str, str], bool],
) -> Any:
    if value is None:
        return value
    parent_table = NULL_ORPHAN_REFERENCES.get(table, {}).get(column)
    if parent_table is None:
        return value
    if _source_reference_exists(source, parent_table, str(value), cache):
        return value
    return None


def is_sqlite_fts_table(name: str, ddl: str) -> bool:
    if name in SQLITE_FTS_TABLES:
        return True
    if any(name.startswith(f"{fts_table}_") for fts_table in SQLITE_FTS_TABLES):
        return True
    return "USING fts5" in ddl


def _source_reference_exists(
    source: sqlite3.Connection,
    table: str,
    value: str,
    cache: dict[tuple[str, str], bool],
) -> bool:
    key = (table, value)
    if key not in cache:
        row = source.execute(
            f"SELECT 1 FROM {_quote_identifier(table)} WHERE id = ? LIMIT 1",
            (value,),
        ).fetchone()
        cache[key] = row is not None
    return cache[key]


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def row_value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]
