"""Regression tests for the PostgreSQL baseline schema artifact."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
POSTGRES_BASELINE_SCHEMA = REPO_ROOT / "src/gobby/storage/postgres_baseline_schema.sql"


def _schema_text() -> str:
    assert POSTGRES_BASELINE_SCHEMA.exists(), (
        "Phase 4 requires a checked-in PostgreSQL baseline at "
        "src/gobby/storage/postgres_baseline_schema.sql"
    )
    return POSTGRES_BASELINE_SCHEMA.read_text(encoding="utf-8")


def _assert_matches(sql: str, pattern: str, message: str) -> None:
    assert re.search(pattern, sql, flags=re.IGNORECASE | re.DOTALL), message


def test_postgres_baseline_schema_file_is_checked_in() -> None:
    assert POSTGRES_BASELINE_SCHEMA.exists()


def test_postgres_baseline_translates_core_sqlite_types() -> None:
    sql = _schema_text()
    upper_sql = sql.upper()

    sqlite_only_fragments = (
        "DATETIME('NOW')",
        "AUTOINCREMENT",
        "CREATE VIRTUAL TABLE",
        "USING FTS5",
        "PRAGMA ",
    )
    for fragment in sqlite_only_fragments:
        assert fragment not in upper_sql

    _assert_matches(sql, r"\bTIMESTAMPTZ\b", "timestamp text columns must become TIMESTAMPTZ")
    _assert_matches(
        sql,
        r"\bTIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+NOW\(\)",
        "datetime('now') defaults must become TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    )
    _assert_matches(sql, r"\bBOOLEAN\b", "SQLite 0/1 integer booleans must become BOOLEAN")
    _assert_matches(sql, r"\bBYTEA\b", "SQLite BLOB columns must become BYTEA")
    _assert_matches(sql, r"\bJSONB\b", "SQLite JSON text columns must become JSONB")
    _assert_matches(
        sql,
        r"INTEGER\s+GENERATED\s+ALWAYS\s+AS\s+IDENTITY\s+PRIMARY\s+KEY",
        "AUTOINCREMENT primary keys must become GENERATED ALWAYS AS IDENTITY",
    )
    _assert_matches(
        sql,
        r"UNIQUE\s+NULLS\s+NOT\s+DISTINCT",
        "COALESCE('__global__') uniqueness must become UNIQUE NULLS NOT DISTINCT",
    )


def test_postgres_baseline_seed_rows_use_now_or_column_defaults() -> None:
    sql = _schema_text()

    assert "datetime('now')" not in sql.lower()
    for placeholder_project in ("_orphaned", "_migrated", "_personal", "_global"):
        assert placeholder_project in sql


def test_postgres_baseline_declares_schema_migrations_only() -> None:
    sql = _schema_text()

    assert "CREATE TABLE schema_version" not in sql
    _assert_matches(
        sql,
        r"CREATE\s+TABLE\s+schema_migrations\s*\([^;]*"
        r"version\s+INTEGER\s+PRIMARY\s+KEY[^;]*"
        r"applied_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+NOW\(\)",
        "Postgres baseline must use schema_migrations with TIMESTAMPTZ applied_at",
    )
    assert "gobby_migration_state" not in sql


def test_postgres_baseline_has_flattened_auth_session_token_hashes() -> None:
    sql = _schema_text()

    _assert_matches(
        sql,
        r"CREATE\s+TABLE\s+auth_sessions\s*\([^;]*token_hash\s+TEXT\s+PRIMARY\s+KEY",
        "auth_sessions must use token_hash as the primary key in the flattened baseline",
    )
    assert "DROP COLUMN token" not in sql
    assert re.search(r"CREATE\s+TABLE\s+auth_sessions", sql, flags=re.IGNORECASE)


def test_postgres_baseline_preserves_nanosecond_span_ranges() -> None:
    sql = _schema_text()

    _assert_matches(
        sql,
        r"CREATE\s+TABLE\s+spans\s*\([^;]*start_time_ns\s+BIGINT\s+NOT\s+NULL",
        "span start_time_ns stores Unix nanoseconds and must use BIGINT",
    )
    _assert_matches(
        sql,
        r"CREATE\s+TABLE\s+spans\s*\([^;]*end_time_ns\s+BIGINT",
        "span end_time_ns stores Unix nanoseconds and must use BIGINT",
    )


def test_postgres_baseline_declares_foreign_keys_deferrable() -> None:
    sql = _schema_text()

    _assert_matches(
        sql,
        r"parent_session_id\s+TEXT\s+REFERENCES\s+sessions\s*\(\s*id\s*\)"
        r"[^,\n]*DEFERRABLE\s+INITIALLY\s+IMMEDIATE",
        "self-referential sessions.parent_session_id must be deferrable",
    )
    _assert_matches(
        sql,
        r"agent_run_id\s+TEXT\s+REFERENCES\s+agent_runs\s*\(\s*id\s*\)"
        r"[^,\n]*DEFERRABLE\s+INITIALLY\s+IMMEDIATE",
        "sessions.agent_run_id -> agent_runs.id must be deferrable",
    )
    _assert_matches(
        sql,
        r"parent_session_id\s+TEXT\s+NOT\s+NULL\s+REFERENCES\s+sessions\s*\(\s*id\s*\)"
        r"[^,\n]*DEFERRABLE\s+INITIALLY\s+IMMEDIATE",
        "agent_runs.parent_session_id -> sessions.id must be deferrable",
    )


def test_postgres_baseline_replaces_fts5_with_pg_search_bm25_indexes() -> None:
    sql = _schema_text()
    upper_sql = sql.upper()

    assert "CREATE EXTENSION" not in upper_sql
    assert "CREATE VIRTUAL TABLE" not in upper_sql
    assert "USING FTS5" not in upper_sql
    for sqlite_fts_table in (
        "tasks_fts",
        "memories_fts",
        "code_symbols_fts",
        "code_content_fts",
        "skills_fts",
    ):
        assert sqlite_fts_table not in sql

    expected_bm25_indexes = {
        "tasks": r"CREATE\s+INDEX\s+\w*tasks\w*bm25\w*\s+ON\s+tasks\s+USING\s+bm25",
        "memories": r"CREATE\s+INDEX\s+\w*memories\w*bm25\w*\s+ON\s+memories\s+USING\s+bm25",
        "code symbols": (
            r"CREATE\s+INDEX\s+\w*code_symbols\w*bm25\w*\s+ON\s+code_symbols\s+USING\s+bm25"
        ),
        "code content": (
            r"CREATE\s+INDEX\s+\w*code_content\w*bm25\w*\s+ON\s+"
            r"code_content(?:_chunks)?\s+USING\s+bm25"
        ),
        "skills": r"CREATE\s+INDEX\s+\w*skills\w*bm25\w*\s+ON\s+skills\s+USING\s+bm25",
    }
    for label, pattern in expected_bm25_indexes.items():
        _assert_matches(sql, pattern, f"{label} must have a pg_search BM25 index")

    assert len(re.findall(r"\bUSING\s+bm25\b", sql, flags=re.IGNORECASE)) == 5
    _assert_matches(
        sql,
        r"WITH\s*\(\s*key_field\s*=\s*'id'\s*\)",
        "pg_search BM25 indexes must declare id as key_field",
    )
    _assert_matches(
        sql,
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+memories_tags_to_text\s*\(\s*tags\s+jsonb\s*\)"
        r".*IMMUTABLE.*PARALLEL\s+SAFE.*RETURNS\s+NULL\s+ON\s+NULL\s+INPUT",
        "memories.tags JSONB must be flattened through an immutable generated-column helper",
    )
    _assert_matches(
        sql,
        r"ADD\s+COLUMN\s+tags_text\s+TEXT\s+GENERATED\s+ALWAYS\s+AS\s*"
        r"\(\s*memories_tags_to_text\s*\(\s*tags\s*\)\s*\)\s+STORED",
        "memories BM25 index needs a stored text column for JSON tags",
    )
    _assert_matches(
        sql,
        r"string_agg\s*\(\s*value\s*,\s*'\s+'\s+ORDER\s+BY\s+ord\s*\)",
        "memories_tags_to_text must aggregate tags deterministically",
    )
