"""Post-copy validation for one-shot SQLite to PostgreSQL migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from gobby.storage.hub.postgres import _PRE_BASELINE_INFRA_TABLES
from gobby.storage.migration.reseed import discover_identity_sequences, expected_sequence_state
from gobby.storage.migrations import BASELINE_VERSION

_POSTGRES_ONLY_TABLES: frozenset[str] = _PRE_BASELINE_INFRA_TABLES | frozenset(
    {"gobby_migration_state", "schema_migrations"}
)
_SQLITE_BOOKKEEPING_TABLES: frozenset[str] = frozenset({"schema_version", "schema_migrations"})
_SQLITE_FTS_TABLES: frozenset[str] = frozenset(
    {
        "tasks_fts",
        "memories_fts",
        "code_symbols_fts",
        "code_content_fts",
        "skills_fts",
    }
)
_CONTENT_HASH_TABLES: frozenset[str] = frozenset(
    {
        "sessions",
        "tasks",
        "memories",
        "config_store",
        "code_symbols",
        "agent_runs",
        "metrics_events",
        "workflow_audit_log",
    }
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Result(Protocol):
    def fetchone(self) -> Any | None: ...
    def fetchall(self) -> Sequence[Any]: ...


class _Executable(Protocol):
    def execute(
        self,
        query: object,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> _Result: ...


@dataclass(frozen=True)
class _Bm25Spec:
    table: str
    index_name: str
    indexed_columns: tuple[str, ...]
    searchable_columns: tuple[str, ...]
    source_sample_columns: tuple[str, ...]


_BM25_INDEXES: tuple[_Bm25Spec, ...] = (
    _Bm25Spec(
        table="tasks",
        index_name="tasks_search_bm25",
        indexed_columns=("id", "title", "description"),
        searchable_columns=("title", "description"),
        source_sample_columns=("title", "description"),
    ),
    _Bm25Spec(
        table="memories",
        index_name="memories_search_bm25",
        indexed_columns=("id", "content", "tags_text"),
        searchable_columns=("content", "tags_text"),
        source_sample_columns=("content", "tags"),
    ),
    _Bm25Spec(
        table="code_symbols",
        index_name="code_symbols_search_bm25",
        indexed_columns=("id", "name", "qualified_name", "signature", "docstring", "summary"),
        searchable_columns=("name", "qualified_name", "signature", "docstring", "summary"),
        source_sample_columns=("name", "qualified_name", "signature", "docstring", "summary"),
    ),
    _Bm25Spec(
        table="code_content_chunks",
        index_name="code_content_search_bm25",
        indexed_columns=("id", "content"),
        searchable_columns=("content",),
        source_sample_columns=("content",),
    ),
    _Bm25Spec(
        table="skills",
        index_name="skills_search_bm25",
        indexed_columns=("id", "name", "description", "content"),
        searchable_columns=("name", "description", "content"),
        source_sample_columns=("name", "description", "content"),
    ),
)


@dataclass(frozen=True)
class ValidationCheckResult:
    name: str
    ok: bool
    message: str
    samples: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class MigrationValidationReport:
    ok: bool
    artifact_path: Path | None
    checks: tuple[ValidationCheckResult, ...]
    source_tables: tuple[str, ...]
    target_tables: tuple[str, ...]
    comparison_tables: tuple[str, ...]


class MigrationValidationError(RuntimeError):
    """Raised when one or more migration validation checks fail."""

    def __init__(
        self,
        failures: Sequence[ValidationCheckResult],
        *,
        artifact_path: Path | None,
    ) -> None:
        message = "; ".join(failure.message for failure in failures)
        if artifact_path is not None:
            message = f"{message}; validation artifact: {artifact_path}"
        super().__init__(message)
        self.failures = tuple(failures)
        self.artifact_path = artifact_path


def validate_migration(
    source: sqlite3.Connection,
    target: _Executable,
    *,
    artifact_dir: Path | None = None,
    emit: Callable[[str], None] | None = None,
) -> MigrationValidationReport:
    """Validate imported PostgreSQL data against the SQLite source."""
    checks: list[ValidationCheckResult] = []
    source_tables = _sqlite_application_tables(source)
    target_tables = _postgres_tables(target)
    target_comparison_tables = target_tables - _POSTGRES_ONLY_TABLES
    comparison_tables = source_tables & target_comparison_tables

    _check_source_schema_baseline(source, checks)
    _check_table_mapping(source_tables, target_comparison_tables, checks)

    source_counts = {table: _sqlite_count(source, table) for table in sorted(comparison_tables)}
    _check_row_counts(source, target, comparison_tables, source_counts, checks)
    _check_content_hashes(source, target, comparison_tables, checks)
    _check_foreign_keys(target, checks)
    _check_sequence_states(target, checks)
    _check_bm25_indexes(source, target, comparison_tables, source_counts, checks)
    _check_check_constraints(target, checks)
    _check_unique_constraints(target, checks)
    _check_not_null_constraints(target, checks)

    artifact_path = _write_artifact(
        checks=checks,
        source_tables=source_tables,
        target_tables=target_tables,
        comparison_tables=comparison_tables,
        artifact_dir=artifact_dir,
    )
    report = MigrationValidationReport(
        ok=all(check.ok for check in checks),
        artifact_path=artifact_path,
        checks=tuple(checks),
        source_tables=tuple(sorted(source_tables)),
        target_tables=tuple(sorted(target_tables)),
        comparison_tables=tuple(sorted(comparison_tables)),
    )
    _emit_results(checks, emit)

    failures = [check for check in checks if not check.ok]
    if failures:
        raise MigrationValidationError(failures, artifact_path=artifact_path)
    return report


def _check_source_schema_baseline(
    source: sqlite3.Connection,
    checks: list[ValidationCheckResult],
) -> None:
    version = _sqlite_schema_version(source)
    if version is None:
        _record(checks, "schema baseline", True, "schema baseline skipped: no version table")
        return
    if version != BASELINE_VERSION:
        _record(
            checks,
            "schema baseline",
            False,
            f"SQLite schema baseline mismatch: expected {BASELINE_VERSION}, found {version}",
        )
        return
    _record(checks, "schema baseline", True, f"SQLite schema baseline v{version}")


def _check_table_mapping(
    source_tables: set[str],
    target_comparison_tables: set[str],
    checks: list[ValidationCheckResult],
) -> None:
    missing = sorted(source_tables - target_comparison_tables)
    unexpected = sorted(target_comparison_tables - source_tables)
    if missing or unexpected:
        parts: list[str] = []
        if missing:
            parts.append(f"missing PostgreSQL tables: {', '.join(missing)}")
        if unexpected:
            parts.append(f"unexpected PostgreSQL-only tables: {', '.join(unexpected)}")
        _record(checks, "table mapping", False, "; ".join(parts))
        return
    _record(checks, "table mapping", True, f"table mapping ok for {len(source_tables)} tables")


def _check_row_counts(
    source: sqlite3.Connection,
    target: _Executable,
    comparison_tables: set[str],
    source_counts: Mapping[str, int],
    checks: list[ValidationCheckResult],
) -> None:
    for table in sorted(comparison_tables):
        source_count = source_counts[table]
        target_count = _postgres_count(target, table)
        if source_count != target_count:
            _record(
                checks,
                f"row count:{table}",
                False,
                f"{table} row count mismatch: SQLite {source_count} != Postgres {target_count}",
            )
            continue
        _record(checks, f"row count:{table}", True, f"{table} row count ok: {source_count}")


def _check_content_hashes(
    source: sqlite3.Connection,
    target: _Executable,
    comparison_tables: set[str],
    checks: list[ValidationCheckResult],
) -> None:
    tables = sorted(comparison_tables & _CONTENT_HASH_TABLES)
    if not tables:
        _record(checks, "content hash", True, "content hash skipped: no representative tables")
        return

    for table in tables:
        source_hash = _table_hash(_sqlite_rows(source, table))
        target_hash = _table_hash(_postgres_rows(target, table, _sqlite_columns(source, table)))
        if source_hash != target_hash:
            _record(
                checks,
                f"content hash:{table}",
                False,
                f"{table} content hash mismatch: SQLite {source_hash} != Postgres {target_hash}",
            )
            continue
        _record(checks, f"content hash:{table}", True, f"{table} content hash ok")


def _check_foreign_keys(target: _Executable, checks: list[ValidationCheckResult]) -> None:
    rows = _catalog_rows(target, _FOREIGN_KEY_SQL)
    if rows is None:
        _record(checks, "foreign keys", True, "foreign key orphan checks skipped: no catalog")
        return

    failures: list[dict[str, object]] = []
    for row in rows:
        table = str(_row_value(row, "table_name"))
        referenced_table = str(_row_value(row, "referenced_table"))
        columns = _string_sequence(_row_value(row, "columns"))
        referenced_columns = _string_sequence(_row_value(row, "referenced_columns"))
        if not columns or len(columns) != len(referenced_columns):
            continue
        count = _foreign_key_orphan_count(
            target, table, referenced_table, columns, referenced_columns
        )
        if count:
            failures.append(
                {
                    "constraint": str(_row_value(row, "constraint_name")),
                    "table": table,
                    "referenced_table": referenced_table,
                    "orphans": count,
                }
            )

    if failures:
        _record(
            checks,
            "foreign keys",
            False,
            f"foreign key orphan checks failed for {len(failures)} constraint(s)",
            failures,
        )
        return
    _record(checks, "foreign keys", True, f"foreign key orphan checks ok: {len(rows)}")


def _check_sequence_states(target: _Executable, checks: list[ValidationCheckResult]) -> None:
    try:
        sequences = discover_identity_sequences(target)
    except (AssertionError, KeyError, IndexError, ValueError):
        _record(checks, "sequence state", True, "sequence state skipped: no identity catalog")
        return

    failures: list[dict[str, object]] = []
    for sequence in sequences:
        max_id = _postgres_max(target, sequence.table_name, sequence.column_name)
        expected_last_value, expected_is_called = expected_sequence_state(max_id)
        state = _sequence_state(target, sequence.sequence_name)
        if state != (expected_last_value, expected_is_called):
            last_value, is_called = state
            failures.append(
                {
                    "table": sequence.table_name,
                    "column": sequence.column_name,
                    "sequence": sequence.sequence_name,
                    "max_id": max_id,
                    "last_value": last_value,
                    "is_called": is_called,
                    "expected_last_value": expected_last_value,
                    "expected_is_called": expected_is_called,
                }
            )

    if failures:
        _record(
            checks,
            "sequence state",
            False,
            f"sequence state mismatch for {len(failures)} sequence(s)",
            failures,
        )
        return
    _record(checks, "sequence state", True, f"sequence state ok: {len(sequences)}")


def _check_bm25_indexes(
    source: sqlite3.Connection,
    target: _Executable,
    comparison_tables: set[str],
    source_counts: Mapping[str, int],
    checks: list[ValidationCheckResult],
) -> None:
    if not _catalog_available(target):
        _record(checks, "bm25 indexes", True, "BM25 checks skipped: no Postgres catalog")
        return

    failures: list[dict[str, object]] = []
    verdicts: list[dict[str, object]] = []
    for spec in _BM25_INDEXES:
        if spec.table not in comparison_tables:
            continue

        exists, definition = _bm25_index_metadata(target, spec.index_name)
        if not exists:
            failures.append({"table": spec.table, "index": spec.index_name, "reason": "missing"})
            continue

        missing_columns = [
            column for column in spec.indexed_columns if definition and column not in definition
        ]
        if missing_columns:
            failures.append(
                {
                    "table": spec.table,
                    "index": spec.index_name,
                    "reason": "missing indexed columns",
                    "columns": missing_columns,
                }
            )
            continue

        source_count = source_counts.get(spec.table, _sqlite_count(source, spec.table))
        if source_count == 0:
            verdicts.append(
                {"table": spec.table, "state": "empty-source", "index": spec.index_name}
            )
            continue

        query = _sample_search_query(source, spec.table, spec.source_sample_columns)
        hits = _bm25_smoke_hits(target, spec, query)
        reads = _bm25_index_reads(target, spec.index_name)
        if hits <= 0 or reads <= 0:
            failures.append(
                {
                    "table": spec.table,
                    "index": spec.index_name,
                    "query": query,
                    "hits": hits,
                    "idx_tup_read": reads,
                }
            )
            continue
        verdicts.append({"table": spec.table, "state": "smoke-query", "hits": hits})

    if failures:
        _record(checks, "bm25 indexes", False, "BM25 index validation failed", failures)
        return
    _record(checks, "bm25 indexes", True, f"BM25 index validation ok: {len(verdicts)}", verdicts)


def _check_check_constraints(target: _Executable, checks: list[ValidationCheckResult]) -> None:
    rows = _catalog_rows(target, _CHECK_CONSTRAINT_SQL)
    if rows is None:
        _record(checks, "check constraints", True, "CHECK constraints skipped: no catalog")
        return

    failures: list[dict[str, object]] = []
    for row in rows:
        table = str(_row_value(row, "table_name"))
        constraint = str(_row_value(row, "constraint_name"))
        expression = str(_row_value(row, "expression"))
        count = _constraint_violation_count(target, table, f"NOT ({expression})")
        if count:
            failures.append(
                {
                    "table": table,
                    "constraint": constraint,
                    "violations": count,
                    "sample_rows": _sample_rows(target, table, f"NOT ({expression})"),
                }
            )

    if failures:
        _record(checks, "check constraints", False, "CHECK constraint violations", failures)
        return
    _record(checks, "check constraints", True, f"CHECK constraints ok: {len(rows)}")


def _check_unique_constraints(target: _Executable, checks: list[ValidationCheckResult]) -> None:
    rows = _catalog_rows(target, _UNIQUE_CONSTRAINT_SQL)
    if rows is None:
        _record(checks, "unique constraints", True, "UNIQUE constraints skipped: no catalog")
        return

    failures: list[dict[str, object]] = []
    for row in rows:
        table = str(_row_value(row, "table_name"))
        constraint = str(_row_value(row, "constraint_name"))
        columns = _string_sequence(_row_value(row, "columns"))
        if not columns:
            continue
        duplicate_groups = _unique_duplicate_groups(target, table, columns)
        if duplicate_groups:
            failures.append(
                {
                    "table": table,
                    "constraint": constraint,
                    "columns": list(columns),
                    "sample_groups": duplicate_groups,
                }
            )

    if failures:
        _record(checks, "unique constraints", False, "UNIQUE constraint duplicates", failures)
        return
    _record(checks, "unique constraints", True, f"UNIQUE constraints ok: {len(rows)}")


def _check_not_null_constraints(target: _Executable, checks: list[ValidationCheckResult]) -> None:
    rows = _catalog_rows(target, _NOT_NULL_SQL, tuple(sorted(_POSTGRES_ONLY_TABLES)))
    if rows is None:
        _record(checks, "not null", True, "NOT NULL constraints skipped: no catalog")
        return

    failures: list[dict[str, object]] = []
    for row in rows:
        table = str(_row_value(row, "table_name"))
        column = str(_row_value(row, "column_name"))
        condition = f"{_quote_identifier(column)} IS NULL"
        count = _constraint_violation_count(target, table, condition)
        if count:
            failures.append(
                {
                    "table": table,
                    "column": column,
                    "nulls": count,
                    "sample_rows": _sample_rows(target, table, condition),
                }
            )

    if failures:
        _record(checks, "not null", False, "NOT NULL constraint violations", failures)
        return
    _record(checks, "not null", True, f"NOT NULL constraints ok: {len(rows)}")


def _sqlite_application_tables(source: sqlite3.Connection) -> set[str]:
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
        name = str(_row_value(row, "name"))
        ddl = str(_row_value(row, "sql") or "")
        if name in _SQLITE_BOOKKEEPING_TABLES or _is_sqlite_fts_table(name, ddl):
            continue
        tables.add(name)
    return tables


def _sqlite_schema_version(source: sqlite3.Connection) -> int | None:
    for table in ("schema_migrations", "schema_version"):
        try:
            row = source.execute(
                f"SELECT MAX(version) AS version FROM {_quote_identifier(table)}"
            ).fetchone()
        except sqlite3.Error:
            continue
        if row is None:
            continue
        version = _row_value(row, "version")
        return int(version) if version is not None else None
    return None


def _postgres_tables(target: _Executable) -> set[str]:
    rows = target.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename"
    ).fetchall()
    return {str(_row_value(row, "tablename", 0)) for row in rows}


def _sqlite_count(source: sqlite3.Connection, table: str) -> int:
    row = source.execute(f"SELECT COUNT(*) AS row_count FROM {_quote_identifier(table)}").fetchone()
    return int(_row_value(row, "row_count"))


def _postgres_count(target: _Executable, table: str) -> int:
    row = target.execute(f"SELECT COUNT(*) AS row_count FROM {_quote_identifier(table)}").fetchone()
    return int(_row_value(row, "row_count"))


def _sqlite_rows(source: sqlite3.Connection, table: str) -> Sequence[Any]:
    return source.execute(f"SELECT * FROM {_quote_identifier(table)}").fetchall()


def _postgres_rows(
    target: _Executable,
    table: str,
    columns: set[str] | None = None,
) -> Sequence[Any]:
    selected = (
        "*" if columns is None else ", ".join(_quote_identifier(column) for column in columns)
    )
    return target.execute(f"SELECT {selected} FROM {_quote_identifier(table)}").fetchall()


def _postgres_max(target: _Executable, table: str, column: str) -> int | None:
    row = target.execute(
        f"SELECT MAX({_quote_identifier(column)}) AS max_id FROM {_quote_identifier(table)}"
    ).fetchone()
    if row is None:
        return None
    value = _row_value(row, "max_id")
    return int(value) if value is not None else None


def _sequence_state(target: _Executable, sequence_name: str) -> tuple[int, bool]:
    row = target.execute(
        f"SELECT last_value, is_called FROM {_quote_qualified_identifier(sequence_name)}"
    ).fetchone()
    if row is None:
        raise MigrationValidationError(
            [
                ValidationCheckResult(
                    name="sequence state",
                    ok=False,
                    message=f"sequence {sequence_name} returned no state row",
                )
            ],
            artifact_path=None,
        )
    return int(_row_value(row, "last_value", 0)), bool(_row_value(row, "is_called", 1))


def _foreign_key_orphan_count(
    target: _Executable,
    table: str,
    referenced_table: str,
    columns: Sequence[str],
    referenced_columns: Sequence[str],
) -> int:
    join_conditions = " AND ".join(
        f"child.{_quote_identifier(column)} = parent.{_quote_identifier(referenced_column)}"
        for column, referenced_column in zip(columns, referenced_columns, strict=True)
    )
    non_null = " AND ".join(f"child.{_quote_identifier(column)} IS NOT NULL" for column in columns)
    first_ref = _quote_identifier(referenced_columns[0])
    row = target.execute(
        f"""
        SELECT COUNT(*) AS violation_count
          FROM {_quote_identifier(table)} AS child
          LEFT JOIN {_quote_identifier(referenced_table)} AS parent
            ON {join_conditions}
         WHERE {non_null}
           AND parent.{first_ref} IS NULL
        """
    ).fetchone()
    return int(_row_value(row, "violation_count"))


def _constraint_violation_count(target: _Executable, table: str, condition: str) -> int:
    row = target.execute(
        f"""
        SELECT COUNT(*) AS violation_count
          FROM {_quote_identifier(table)}
         WHERE {condition}
        """
    ).fetchone()
    return int(_row_value(row, "violation_count"))


def _sample_rows(target: _Executable, table: str, condition: str) -> list[dict[str, object]]:
    rows = target.execute(
        f"""
        SELECT *
          FROM {_quote_identifier(table)}
         WHERE {condition}
         LIMIT 5
        """
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _unique_duplicate_groups(
    target: _Executable,
    table: str,
    columns: Sequence[str],
) -> list[dict[str, object]]:
    column_sql = ", ".join(_quote_identifier(column) for column in columns)
    non_null = " AND ".join(f"{_quote_identifier(column)} IS NOT NULL" for column in columns)
    rows = target.execute(
        f"""
        SELECT {column_sql}, COUNT(*) AS duplicate_count
          FROM {_quote_identifier(table)}
         WHERE {non_null}
         GROUP BY {column_sql}
        HAVING COUNT(*) > 1
         LIMIT 5
        """
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _bm25_index_metadata(target: _Executable, index_name: str) -> tuple[bool, str]:
    row = target.execute(
        """
        SELECT pg_get_indexdef(c.oid) AS index_definition
          FROM pg_class AS c
         WHERE c.relkind = 'i'
           AND c.relname = %s
         LIMIT 1
        """,
        (index_name,),
    ).fetchone()
    if row is None:
        return False, ""
    return True, str(_row_value(row, "index_definition"))


def _bm25_smoke_hits(target: _Executable, spec: _Bm25Spec, query: str) -> int:
    if not query:
        return 0
    clauses = " OR ".join(
        f"{_quote_identifier(column)} @@@ %s" for column in spec.searchable_columns
    )
    params = tuple(query for _ in spec.searchable_columns)
    row = target.execute(
        f"SELECT COUNT(*) AS hits FROM {_quote_identifier(spec.table)} WHERE {clauses}",
        params,
    ).fetchone()
    return int(_row_value(row, "hits"))


def _bm25_index_reads(target: _Executable, index_name: str) -> int:
    row = target.execute(
        """
        SELECT COALESCE(idx_tup_read, 0) AS idx_tup_read
          FROM pg_stat_user_indexes
         WHERE indexrelname = %s
         LIMIT 1
        """,
        (index_name,),
    ).fetchone()
    if row is None:
        return 0
    return int(_row_value(row, "idx_tup_read"))


def _sample_search_query(
    source: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> str:
    existing_columns = _sqlite_columns(source, table)
    selected_columns = [column for column in columns if column in existing_columns]
    if not selected_columns:
        return ""
    column_sql = ", ".join(_quote_identifier(column) for column in selected_columns)
    row = source.execute(f"SELECT {column_sql} FROM {_quote_identifier(table)} LIMIT 1").fetchone()
    if row is None:
        return ""

    pieces = [
        str(_jsonable(_row_value(row, column))).strip()
        for column in selected_columns
        if _row_value(row, column) not in (None, "")
    ]
    tokens = " ".join(pieces).split()
    if not tokens:
        return ""
    return _sanitize_pg_search_query(tokens[0])


def _sqlite_columns(source: sqlite3.Connection, table: str) -> set[str]:
    rows = source.execute(f"PRAGMA table_info({_quote_sqlite_string(table)})").fetchall()
    return {str(_row_value(row, "name", 1)) for row in rows}


def _table_hash(rows: Sequence[Any]) -> str:
    encoded_rows = [
        json.dumps(_row_to_dict(row), sort_keys=True, separators=(",", ":"), default=str)
        for row in rows
    ]
    digest = hashlib.sha256()
    for encoded in sorted(encoded_rows):
        digest.update(encoded.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _row_to_dict(row: Any) -> dict[str, object]:
    if isinstance(row, Mapping):
        return {str(key): _jsonable(value) for key, value in row.items()}
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {str(key): _jsonable(row[key]) for key in keys()}
    if isinstance(row, Sequence) and not isinstance(row, str | bytes | bytearray):
        return {str(index): _jsonable(value) for index, value in enumerate(row)}
    return {"value": _jsonable(row)}


def _jsonable(value: Any) -> object:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return {"__bytes__": bytes(value).hex()}
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _jsonable(json.loads(stripped))
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    return value


def _catalog_available(target: _Executable) -> bool:
    rows = _catalog_rows(target, "SELECT 1 FROM pg_class LIMIT 1")
    return rows is not None


def _catalog_rows(
    target: _Executable,
    query: str,
    params: Sequence[Any] | Mapping[str, Any] = (),
) -> Sequence[Any] | None:
    try:
        return target.execute(query, params).fetchall()
    except (AssertionError, KeyError, IndexError, ValueError):
        return None


def _string_sequence(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip("{}")
        if not stripped:
            return ()
        return tuple(item.strip('"') for item in stripped.split(","))
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return (str(value),)


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]


def _is_sqlite_fts_table(name: str, ddl: str) -> bool:
    if name in _SQLITE_FTS_TABLES:
        return True
    if any(name.startswith(f"{fts_table}_") for fts_table in _SQLITE_FTS_TABLES):
        return True
    return "USING fts5" in ddl


def _record(
    checks: list[ValidationCheckResult],
    name: str,
    ok: bool,
    message: str,
    samples: Sequence[Mapping[str, object]] = (),
) -> None:
    checks.append(
        ValidationCheckResult(
            name=name,
            ok=ok,
            message=message,
            samples=[dict(sample) for sample in samples],
        )
    )


def _emit_results(
    checks: Sequence[ValidationCheckResult],
    emit: Callable[[str], None] | None,
) -> None:
    sink = emit or print
    for check in checks:
        marker = "✓" if check.ok else "✗"
        sink(f"{marker} {check.message}")


def _write_artifact(
    *,
    checks: Sequence[ValidationCheckResult],
    source_tables: set[str],
    target_tables: set[str],
    comparison_tables: set[str],
    artifact_dir: Path | None,
) -> Path | None:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = artifact_dir or _default_artifact_dir()
    path = root / f"validate-{timestamp}.json"
    payload: dict[str, object] = {
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "ok": all(check.ok for check in checks),
        "source_tables": sorted(source_tables),
        "target_tables": sorted(target_tables),
        "comparison_tables": sorted(comparison_tables),
        "checks": [asdict(check) for check in checks],
    }
    try:
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    except OSError:
        return None
    return path


def _default_artifact_dir() -> Path:
    gobby_home = os.getenv("GOBBY_HOME")
    if gobby_home:
        return Path(gobby_home).expanduser() / "migrations"
    return Path.home() / ".gobby" / "migrations"


def _quote_identifier(identifier: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(identifier):
        return '"' + identifier.replace('"', '""') + '"'
    return f'"{identifier}"'


def _quote_qualified_identifier(identifier: str) -> str:
    return ".".join(_quote_identifier(part) for part in identifier.split("."))


def _quote_sqlite_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sanitize_pg_search_query(query: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in (" ", "_", "-") else " " for ch in query)
    return " ".join(token for token in cleaned.split() if token)


_FOREIGN_KEY_SQL = """
SELECT
    con.conname AS constraint_name,
    rel.relname AS table_name,
    ref.relname AS referenced_table,
    array_agg(att.attname ORDER BY keys.ordinality) AS columns,
    array_agg(ref_att.attname ORDER BY keys.ordinality) AS referenced_columns
FROM pg_constraint AS con
JOIN pg_class AS rel ON rel.oid = con.conrelid
JOIN pg_class AS ref ON ref.oid = con.confrelid
JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY
    AS keys(attnum, ref_attnum, ordinality) ON true
JOIN pg_attribute AS att ON att.attrelid = con.conrelid AND att.attnum = keys.attnum
JOIN pg_attribute AS ref_att ON ref_att.attrelid = con.confrelid
    AND ref_att.attnum = keys.ref_attnum
WHERE con.contype = 'f'
  AND con.connamespace = current_schema()::regnamespace
GROUP BY con.conname, rel.relname, ref.relname
ORDER BY rel.relname, con.conname
"""

_CHECK_CONSTRAINT_SQL = """
SELECT
    con.conname AS constraint_name,
    rel.relname AS table_name,
    pg_get_expr(con.conbin, con.conrelid) AS expression
FROM pg_constraint AS con
JOIN pg_class AS rel ON rel.oid = con.conrelid
WHERE con.contype = 'c'
  AND con.connamespace = current_schema()::regnamespace
ORDER BY rel.relname, con.conname
"""

_UNIQUE_CONSTRAINT_SQL = """
SELECT
    con.conname AS constraint_name,
    rel.relname AS table_name,
    array_agg(att.attname ORDER BY keys.ordinality) AS columns
FROM pg_constraint AS con
JOIN pg_class AS rel ON rel.oid = con.conrelid
JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS keys(attnum, ordinality) ON true
JOIN pg_attribute AS att ON att.attrelid = con.conrelid AND att.attnum = keys.attnum
WHERE con.contype = 'u'
  AND con.connamespace = current_schema()::regnamespace
GROUP BY con.conname, rel.relname
ORDER BY rel.relname, con.conname
"""

_NOT_NULL_SQL = """
SELECT table_name, column_name
  FROM information_schema.columns
 WHERE table_schema = current_schema()
   AND is_nullable = 'NO'
   AND table_name <> ALL(%s)
 ORDER BY table_name, ordinal_position
"""
