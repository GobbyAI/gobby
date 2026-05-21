"""One-shot SQLite to PostgreSQL hub migration."""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Literal, cast

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from gobby.config.postgres_bootstrap import active_install_mode
from gobby.storage.hub.postgres import PostgresHubDatabase, _classify_baseline_state
from gobby.storage.migration.reseed import reseed_identity_sequences
from gobby.storage.migration.schema import validate_sqlite_source_schema
from gobby.storage.migration.validation import (
    _BM25_INDEXES,
    _POSTGRES_ONLY_TABLES,
    MigrationValidationError,
    _postgres_count,
    _postgres_tables,
    _sqlite_application_tables,
    _sqlite_count,
    validate_migration,
)
from gobby.storage.migrations import MigrationUnsupportedError

_BASELINE_INSERT_RE = re.compile(r'^\s*INSERT\s+INTO\s+"?([A-Za-z_][A-Za-z0-9_]*)"?', re.I | re.M)
_IMPORT_COMPLETE_KEY = "imported_from_sqlite_at"
_IMPORT_LOCK_KEY = "gobby_sqlite_to_postgres_import"
_EXTERNAL_SENTINEL_MISSING = (
    "external-ownership sentinel missing -- was the database recreated or is this a "
    "different install? Re-run `gobby postgres install --mode external --dsn ...` to "
    "recreate the sentinel, then restart the import."
)
_OK = "\u2713"
_FAIL = "\u2717"
_InstallMode = Literal["docker", "native", "external"]


@cache
def _seed_bearing_tables() -> tuple[str, ...]:
    baseline_sql = (
        importlib.resources.files("gobby.storage")
        .joinpath("postgres_baseline_schema.sql")
        .read_text()
    )
    return tuple(sorted(set(_BASELINE_INSERT_RE.findall(baseline_sql)) - _POSTGRES_ONLY_TABLES))


class SqliteToPostgresMigrationError(RuntimeError):
    """Raised when the SQLite to PostgreSQL migration cannot safely continue."""


@dataclass(frozen=True)
class _CopyResult:
    rows: int
    tables: int


@contextmanager
def _open_sqlite_source(source: Path) -> Iterator[sqlite3.Connection]:
    uri = f"{source.expanduser().resolve().as_uri()}?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise SqliteToPostgresMigrationError(
            f"Unable to open SQLite source read-only: {source}"
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _connect_postgres(target: str) -> Iterator[psycopg.Connection[Any]]:
    try:
        with psycopg.connect(
            target,
            autocommit=True,
            connect_timeout=5,
            row_factory=dict_row,
        ) as conn:
            yield conn
    except psycopg.Error as exc:
        raise SqliteToPostgresMigrationError(f"Unable to connect to PostgreSQL: {exc}") from exc


def migrate_sqlite_to_postgres(
    *,
    source: Path,
    target: str,
    batch_size: int = 1000,
    dry_run: bool = False,
    reset_seeded_tables: bool = True,
    emit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Migrate a SQLite hub database into an initialized PostgreSQL target."""
    if batch_size < 1:
        raise SqliteToPostgresMigrationError("batch_size must be at least 1")

    sink = emit or print
    source_path = source.expanduser()
    with _open_sqlite_source(source_path) as source_conn:
        _assert_source_schema_supported(source_conn)
        install_mode = active_install_mode()

        if dry_run:
            return _run_dry_run(source_conn, target, install_mode=install_mode, emit=sink)

        return _run_import(
            source_conn,
            target,
            batch_size=batch_size,
            reset_seeded_tables=reset_seeded_tables,
            install_mode=install_mode,
            emit=sink,
        )


def _run_dry_run(
    source: sqlite3.Connection,
    target: str,
    *,
    install_mode: _InstallMode,
    emit: Callable[[str], None],
) -> dict[str, Any]:
    with _connect_postgres(target) as pg:
        _run_target_read_only_preflight(pg, install_mode=install_mode, emit=emit)
        counts = _run_table_mapping_preflight(source, pg, emit=emit)
    return _migration_result(
        rows=sum(counts.values()),
        tables=len(counts),
        dry_run=True,
        log_path=None,
        validation_artifact=None,
    )


def _run_import(
    source: sqlite3.Connection,
    target: str,
    *,
    batch_size: int,
    reset_seeded_tables: bool,
    install_mode: _InstallMode,
    emit: Callable[[str], None],
) -> dict[str, Any]:
    with _connect_postgres(target) as pg:
        _run_target_read_only_preflight(pg, install_mode=install_mode, emit=emit)

    _apply_postgres_schema(target)
    log_path = _default_import_log_path()
    with _connect_postgres(target) as pg:
        copy_result, validation_artifact = _copy_validate_and_mark(
            source,
            pg,
            batch_size=batch_size,
            log_path=log_path,
            reset_seeded_tables=reset_seeded_tables,
            emit=emit,
        )

    return _migration_result(
        rows=copy_result.rows,
        tables=copy_result.tables,
        dry_run=False,
        log_path=log_path,
        validation_artifact=validation_artifact,
    )


def _copy_validate_and_mark(
    source: sqlite3.Connection,
    target: psycopg.Connection[Any],
    *,
    batch_size: int,
    log_path: Path,
    reset_seeded_tables: bool,
    emit: Callable[[str], None],
) -> tuple[_CopyResult, Path | None]:
    with target.transaction():
        _acquire_import_lock(target)
        _assert_target_ready_for_import(source, target, emit=emit)
        _fail_if_import_complete_marker(target)
        if reset_seeded_tables:
            _reset_seed_bearing_tables(target)
        _drop_bm25_indexes(target)
        copy_result = _copy_sqlite_rows_to_postgres(source, target, batch_size, log_path)
        _recreate_bm25_indexes(target)
        reseed_identity_sequences(target)
        try:
            report = validate_migration(source, cast(Any, target), emit=emit)
        except MigrationValidationError as exc:
            raise SqliteToPostgresMigrationError(str(exc)) from exc
        _write_import_complete_marker(target)
    return copy_result, report.artifact_path


def _migration_result(
    *,
    rows: int,
    tables: int,
    dry_run: bool,
    log_path: Path | None,
    validation_artifact: Path | None,
) -> dict[str, Any]:
    return {
        "rows": rows,
        "tables": tables,
        "dry_run": dry_run,
        "log_path": str(log_path) if log_path else None,
        "validation_artifact": str(validation_artifact) if validation_artifact else None,
    }


def _assert_source_schema_supported(source: sqlite3.Connection) -> None:
    try:
        result = validate_sqlite_source_schema(source)
    except sqlite3.Error as exc:
        raise SqliteToPostgresMigrationError(
            "SQLite source is not a readable Gobby database"
        ) from exc
    if not result.ok:
        raise SqliteToPostgresMigrationError(result.message)


def _run_target_read_only_preflight(
    target: psycopg.Connection[Any],
    *,
    install_mode: _InstallMode,
    emit: Callable[[str], None],
) -> None:
    _emit_check(emit, True, "Postgres connectivity ok")
    _probe_pg_search_extension(target, emit=emit)
    state = _classify_baseline_state(target)
    _emit_check(emit, True, f"Postgres baseline state: {state}")
    if install_mode == "external":
        _probe_external_ownership_sentinel(target, emit=emit)


def _probe_pg_search_extension(
    target: psycopg.Connection[Any],
    *,
    emit: Callable[[str], None],
) -> None:
    row = target.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_search'").fetchone()
    if row is None:
        _emit_check(emit, False, "pg_search extension missing")
        raise SqliteToPostgresMigrationError("pg_search extension is not present on target")
    _emit_check(emit, True, "pg_search extension present")


def _probe_external_ownership_sentinel(
    target: psycopg.Connection[Any],
    *,
    emit: Callable[[str], None],
) -> None:
    try:
        row = target.execute("SELECT 1 FROM gobby_install_ownership WHERE id = 1").fetchone()
    except psycopg.Error as exc:
        _emit_check(emit, False, _EXTERNAL_SENTINEL_MISSING)
        raise SqliteToPostgresMigrationError(_EXTERNAL_SENTINEL_MISSING) from exc
    if row is None:
        _emit_check(emit, False, _EXTERNAL_SENTINEL_MISSING)
        raise SqliteToPostgresMigrationError(_EXTERNAL_SENTINEL_MISSING)
    _emit_check(emit, True, "external ownership sentinel present")


def _run_table_mapping_preflight(
    source: sqlite3.Connection,
    target: psycopg.Connection[Any],
    *,
    emit: Callable[[str], None],
) -> dict[str, int]:
    source_tables = _sqlite_application_tables(source)
    target_tables = _postgres_tables(cast(Any, target))
    target_comparison_tables = target_tables - _POSTGRES_ONLY_TABLES
    postgres_only = sorted(target_tables & _POSTGRES_ONLY_TABLES)
    _emit_check(emit, True, f"Postgres-only exclusions ok: {', '.join(postgres_only) or 'none'}")

    missing = sorted(source_tables - target_comparison_tables)
    unexpected = sorted(target_comparison_tables - source_tables)
    if missing or unexpected:
        parts: list[str] = []
        if missing:
            parts.append(f"missing PostgreSQL tables: {', '.join(missing)}")
        if unexpected:
            parts.append(f"unexpected PostgreSQL tables: {', '.join(unexpected)}")
        message = "; ".join(parts)
        _emit_check(emit, False, message)
        raise SqliteToPostgresMigrationError(message)

    counts = {table: _sqlite_count(source, table) for table in sorted(source_tables)}
    _emit_check(emit, True, f"table mapping ok for {len(source_tables)} tables")
    _emit_check(
        emit,
        True,
        f"source row counts enumerated: {sum(counts.values())} rows across {len(counts)} tables",
    )
    return counts


def _apply_postgres_schema(target: str) -> None:
    db = PostgresHubDatabase(target)
    try:
        db.apply_migrations()
    except MigrationUnsupportedError as exc:
        raise SqliteToPostgresMigrationError(str(exc)) from exc
    finally:
        db.close()


def _assert_target_ready_for_import(
    source: sqlite3.Connection,
    target: psycopg.Connection[Any],
    *,
    emit: Callable[[str], None],
) -> None:
    counts = _run_table_mapping_preflight(source, target, emit=emit)
    occupied = [
        table
        for table in sorted(counts)
        if table not in _seed_bearing_tables() and _postgres_count(cast(Any, target), table) > 0
    ]
    if occupied:
        message = (
            "PostgreSQL target is not empty; reset it with the mode-specific recovery "
            f"runbook before importing: {', '.join(occupied)}"
        )
        _emit_check(emit, False, message)
        raise SqliteToPostgresMigrationError(message)
    _emit_check(emit, True, "target application tables are empty apart from canonical seeds")


def _fail_if_import_complete_marker(target: psycopg.Connection[Any]) -> None:
    row = target.execute(
        "SELECT value FROM gobby_migration_state WHERE key = %s",
        (_IMPORT_COMPLETE_KEY,),
    ).fetchone()
    if row is not None:
        raise SqliteToPostgresMigrationError(
            "PostgreSQL target already has imported_from_sqlite_at marker; reset the target "
            "using the mode-specific recovery runbook before re-running the importer."
        )


def _acquire_import_lock(target: psycopg.Connection[Any]) -> None:
    target.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_IMPORT_LOCK_KEY,))


def _reset_seed_bearing_tables(target: psycopg.Connection[Any]) -> None:
    with target.transaction():
        with target.cursor() as cur:
            for table in _seed_bearing_tables():
                cur.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(sql.Identifier(table))
                )


def _drop_bm25_indexes(target: psycopg.Connection[Any]) -> None:
    with target.transaction():
        with target.cursor() as cur:
            for spec in _BM25_INDEXES:
                cur.execute(
                    sql.SQL("DROP INDEX IF EXISTS {}").format(sql.Identifier(spec.index_name))
                )


def _recreate_bm25_indexes(target: psycopg.Connection[Any]) -> None:
    with target.transaction():
        with target.cursor() as cur:
            for spec in _BM25_INDEXES:
                cur.execute(
                    sql.SQL("CREATE INDEX {} ON {} USING bm25 ({}) WITH (key_field='id')").format(
                        sql.Identifier(spec.index_name),
                        sql.Identifier(spec.table),
                        sql.SQL(", ").join(
                            sql.Identifier(column) for column in spec.indexed_columns
                        ),
                    )
                )


def _copy_sqlite_rows_to_postgres(
    source: sqlite3.Connection,
    target: psycopg.Connection[Any],
    batch_size: int,
    log_path: Path,
) -> _CopyResult:
    tables = _dependency_ordered_tables(source, _sqlite_application_tables(source))
    total_rows = 0
    copied_tables = 0
    with target.transaction():
        target.execute("SET CONSTRAINTS ALL DEFERRED")
        for table in tables:
            columns = _copy_columns(source, target, table)
            _write_import_log(log_path, {"event": "table_copy_start", "table": table})
            row_count = _copy_table(source, target, table, columns, batch_size)
            _write_import_log(
                log_path,
                {"event": "table_copy_end", "table": table, "rows": row_count},
            )
            total_rows += row_count
            copied_tables += 1
        target.execute("SET CONSTRAINTS ALL IMMEDIATE")
    return _CopyResult(rows=total_rows, tables=copied_tables)


def _copy_table(
    source: sqlite3.Connection,
    target: psycopg.Connection[Any],
    table: str,
    columns: Sequence[str],
    batch_size: int,
) -> int:
    query = f"SELECT {_identifier_list(columns)} FROM {_quote_identifier(table)}"
    copy_query = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
    )
    count = 0
    source_cursor = source.execute(query)
    with closing(source_cursor):
        with target.cursor() as pg_cursor:
            with pg_cursor.copy(copy_query) as copy:
                while batch := source_cursor.fetchmany(batch_size):
                    for row in batch:
                        copy.write_row(tuple(row[column] for column in columns))
                        count += 1
    return count


def _copy_columns(
    source: sqlite3.Connection,
    target: psycopg.Connection[Any],
    table: str,
) -> tuple[str, ...]:
    source_columns = _sqlite_columns(source, table)
    target_columns = _postgres_insertable_columns(target, table)
    missing = [column for column in source_columns if column not in target_columns]
    if missing:
        raise SqliteToPostgresMigrationError(
            f"PostgreSQL table {table} is missing SQLite columns: {', '.join(missing)}"
        )
    columns = tuple(column for column in source_columns if column in target_columns)
    if not columns:
        raise SqliteToPostgresMigrationError(f"No importable columns for table {table}")
    return columns


def _sqlite_columns(source: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = source.execute(f"PRAGMA table_info({_quote_sqlite_string(table)})").fetchall()
    return tuple(str(row["name"]) for row in rows)


def _postgres_insertable_columns(
    target: psycopg.Connection[Any],
    table: str,
) -> set[str]:
    rows = target.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
           AND is_generated = 'NEVER'
         ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return {str(row["column_name"]) for row in rows}


def _dependency_ordered_tables(
    source: sqlite3.Connection,
    tables: set[str],
) -> tuple[str, ...]:
    dependencies = {
        table: _sqlite_foreign_key_dependencies(source, table) & tables for table in tables
    }
    ordered: list[str] = []
    remaining = {table: set(deps) for table, deps in dependencies.items()}
    while remaining:
        ready = sorted(table for table, deps in remaining.items() if not deps)
        if not ready:
            ordered.extend(sorted(remaining))
            break
        ordered.extend(ready)
        for table in ready:
            remaining.pop(table, None)
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(ordered)


def _sqlite_foreign_key_dependencies(source: sqlite3.Connection, table: str) -> set[str]:
    rows = source.execute(f"PRAGMA foreign_key_list({_quote_sqlite_string(table)})").fetchall()
    return {str(row["table"]) for row in rows}


def _write_import_complete_marker(target: psycopg.Connection[Any]) -> None:
    with target.transaction():
        target.execute(
            "INSERT INTO gobby_migration_state (key, value) VALUES (%s, NOW()::text)",
            (_IMPORT_COMPLETE_KEY,),
        )


def _default_import_log_path() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return _default_migration_dir() / f"import-{timestamp}.log"


def _default_migration_dir() -> Path:
    gobby_home = os.getenv("GOBBY_HOME")
    if gobby_home:
        return Path(gobby_home).expanduser() / "migrations"
    return Path.home() / ".gobby" / "migrations"


def _write_import_log(log_path: Path, record: Mapping[str, object]) -> None:
    payload = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        **dict(record),
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _emit_check(emit: Callable[[str], None], ok: bool, message: str) -> None:
    emit(f"{_OK if ok else _FAIL} {message}")


def _identifier_list(columns: Sequence[str]) -> str:
    return ", ".join(_quote_identifier(column) for column in columns)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_sqlite_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
