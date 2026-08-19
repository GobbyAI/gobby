"""Regression tests for the PostgreSQL baseline schema artifact."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
POSTGRES_BASELINE_SCHEMA = REPO_ROOT / "crates/gcore/assets/schema/baseline.sql"
BAD_TIMESTAMP_DECLARATION_RE = re.compile(
    r"^\s*(?:ADD\s+COLUMN\s+)?(?P<column>[a-z_][a-z0-9_]*)\s+"
    r"TIMESTAMP(?:\s*\(\d+\))?(?:\s+WITHOUT\s+TIME\s+ZONE)?\b"
    r"(?!\s+WITH\s+TIME\s+ZONE)",
    flags=re.IGNORECASE | re.MULTILINE,
)
TEXT_COLUMN_DECLARATION_RE = re.compile(
    r"^\s*(?:ADD\s+COLUMN\s+)?(?P<column>[a-z_][a-z0-9_]*)\s+TEXT\b",
    flags=re.IGNORECASE | re.MULTILINE,
)
TIMESTAMP_LIKE_TEXT_COLUMN_RE = re.compile(
    r"(?:^timestamp$|_at$|_time$|_timestamp$|^(?:first_seen|last_seen)$)",
    flags=re.IGNORECASE,
)


def _schema_text() -> str:
    assert POSTGRES_BASELINE_SCHEMA.exists(), (
        "gcore requires a checked-in PostgreSQL baseline at crates/gcore/assets/schema/baseline.sql"
    )
    return POSTGRES_BASELINE_SCHEMA.read_text(encoding="utf-8")


def _assert_matches(sql: str, pattern: str, message: str) -> None:
    assert re.search(pattern, sql, flags=re.IGNORECASE | re.DOTALL), message


def _timestamp_like_text_columns(sql: str) -> list[str]:
    return sorted(
        match.group("column")
        for match in TEXT_COLUMN_DECLARATION_RE.finditer(sql)
        if TIMESTAMP_LIKE_TEXT_COLUMN_RE.search(match.group("column"))
    )


def test_postgres_baseline_schema_file_is_checked_in() -> None:
    assert POSTGRES_BASELINE_SCHEMA.exists()


def test_postgres_baseline_contains_no_gwiki_owned_objects() -> None:
    assert "gwiki_" not in _schema_text().lower()


def test_postgres_baseline_uses_native_types() -> None:
    sql = _schema_text()
    upper_sql = sql.upper()

    removed_backend_fragments = (
        "DATETIME('NOW')",
        "AUTOINCREMENT",
        "CREATE VIRTUAL TABLE",
        "USING FTS5",
        "PRA" + "GMA ",
    )
    for fragment in removed_backend_fragments:
        assert fragment not in upper_sql

    _assert_matches(sql, r"\bTIMESTAMPTZ\b", "timestamp text columns must become TIMESTAMPTZ")
    _assert_matches(
        sql,
        r"\bTIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+NOW\(\)",
        "datetime('now') defaults must become TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    )
    _assert_matches(sql, r"\bBOOLEAN\b", "integer booleans must become BOOLEAN")
    _assert_matches(sql, r"\bBYTEA\b", "binary columns must become BYTEA")
    _assert_matches(sql, r"\bJSONB\b", "JSON text columns must become JSONB")
    _assert_matches(
        sql,
        r"ALTER\s+TABLE\s+\w+\s+ALTER\s+COLUMN\s+id\s+"
        r"ADD\s+GENERATED\s+ALWAYS\s+AS\s+IDENTITY",
        "AUTOINCREMENT primary keys must become GENERATED ALWAYS AS IDENTITY",
    )
    _assert_matches(
        sql,
        r"UNIQUE\s+NULLS\s+NOT\s+DISTINCT",
        "COALESCE('__global__') uniqueness must become UNIQUE NULLS NOT DISTINCT",
    )


def test_postgres_baseline_rejects_timestamp_without_time_zone_columns() -> None:
    bad_columns = [
        match.group("column") for match in BAD_TIMESTAMP_DECLARATION_RE.finditer(_schema_text())
    ]

    assert bad_columns == []


def test_postgres_baseline_rejects_text_datetime_columns() -> None:
    assert _timestamp_like_text_columns(_schema_text()) == []


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
        r"version\s+INTEGER\s+NOT\s+NULL[^;]*"
        r"applied_at\s+TIMESTAMP\s+WITH\s+TIME\s+ZONE\s+DEFAULT\s+NOW\(\)\s+NOT\s+NULL",
        "Postgres baseline must use schema_migrations with TIMESTAMPTZ applied_at",
    )
    _assert_matches(
        sql,
        r"ADD\s+CONSTRAINT\s+schema_migrations_pkey\s+PRIMARY\s+KEY\s*\(version\)",
        "schema_migrations.version must remain the primary key",
    )
    assert "gobby_migration_state" not in sql


def test_postgres_baseline_has_flattened_auth_session_token_hashes() -> None:
    sql = _schema_text()

    _assert_matches(
        sql,
        r"CREATE\s+TABLE\s+auth_sessions\s*\([^;]*token_hash\s+TEXT\s+NOT\s+NULL",
        "auth_sessions must keep a NOT NULL token_hash column",
    )
    _assert_matches(
        sql,
        r"ADD\s+CONSTRAINT\s+auth_sessions_pkey\s+PRIMARY\s+KEY\s*\(id\)",
        "auth_sessions.id must be the primary key",
    )
    _assert_matches(
        sql,
        r"ADD\s+CONSTRAINT\s+auth_sessions_token_hash_key\s+UNIQUE\s*\(token_hash\)",
        "auth_sessions.token_hash must stay unique",
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
        r"sessions_parent_session_id_fkey\s+FOREIGN\s+KEY\s*\(parent_session_id\)\s+"
        r"REFERENCES\s+sessions\s*\(id\)\s+DEFERRABLE",
        "self-referential sessions.parent_session_id must be deferrable",
    )
    _assert_matches(
        sql,
        r"sessions_agent_run_id_fkey\s+FOREIGN\s+KEY\s*\(agent_run_id\)\s+"
        r"REFERENCES\s+agent_runs\s*\(id\)\s+ON\s+DELETE\s+SET\s+NULL\s+DEFERRABLE",
        "sessions.agent_run_id -> agent_runs.id must be deferrable",
    )
    _assert_matches(
        sql,
        r"agent_runs_parent_session_id_fkey\s+FOREIGN\s+KEY\s*\(parent_session_id\)\s+"
        r"REFERENCES\s+sessions\s*\(id\)\s+DEFERRABLE",
        "agent_runs.parent_session_id -> sessions.id must be deferrable",
    )


def test_postgres_baseline_replaces_fts5_with_pg_search_bm25_indexes() -> None:
    sql = _schema_text()
    upper_sql = sql.upper()

    assert "CREATE EXTENSION IF NOT EXISTS PGCRYPTO" in upper_sql
    assert "CREATE VIRTUAL TABLE" not in upper_sql
    assert "USING FTS5" not in upper_sql
    for removed_fts_table in (
        "tasks_fts",
        "memories_fts",
        "code_symbols_fts",
        "code_content_fts",
        "skills_fts",
    ):
        assert removed_fts_table not in sql

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
        "tool result chunks": (
            r"CREATE\s+INDEX\s+\w*tool_result_chunks\w*bm25\w*\s+ON\s+"
            r"tool_result_chunks\s+USING\s+bm25"
        ),
    }
    for label, pattern in expected_bm25_indexes.items():
        _assert_matches(sql, pattern, f"{label} must have a pg_search BM25 index")

    assert len(re.findall(r"\bUSING\s+bm25\b", sql, flags=re.IGNORECASE)) == 6
    _assert_matches(
        sql,
        r"WITH\s*\(\s*key_field\s*=\s*'?id'?\s*\)",
        "pg_search BM25 indexes must declare id as key_field",
    )
    _assert_matches(
        sql,
        r"CREATE(?:\s+OR\s+REPLACE)?\s+FUNCTION\s+memories_tags_to_text\s*"
        r"\(\s*tags\s+jsonb\s*\).*IMMUTABLE\s+STRICT\s+PARALLEL\s+SAFE",
        "memories.tags JSONB must be flattened through an immutable generated-column helper",
    )
    _assert_matches(
        sql,
        r"tags_text\s+TEXT\s+GENERATED\s+ALWAYS\s+AS\s*"
        r"\(\s*memories_tags_to_text\s*\(\s*tags\s*\)\s*\)\s+STORED",
        "memories BM25 index needs a stored text column for JSON tags",
    )
    _assert_matches(
        sql,
        r"string_agg\s*\(\s*value\s*,\s*'\s+'\s+ORDER\s+BY\s+ord\s*\)",
        "memories_tags_to_text must aggregate tags deterministically",
    )
