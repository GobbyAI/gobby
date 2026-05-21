"""PostgreSQL implementation of the hub database protocol."""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Literal, cast

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gobby.storage.hub._ambient import ambient_transaction, enter_transaction
from gobby.storage.hub.placeholders import (
    params_from_indexes as _params_from_indexes,
)
from gobby.storage.hub.placeholders import (
    remap_dollar_placeholders,
    remap_qmark_placeholders,
    scan_dollar_placeholder_indexes,
    scan_qmark_placeholder_indexes,
)
from gobby.storage.hub.protocol import (
    ChatAttachmentMutation,
    Cursor,
    DispatchMutexRow,
    LockAcquisitionOrderError,
    LockTarget,
    Row,
    Savepoint,
    SessionRecoveryByProject,
    SessionRegistration,
    SystemSessionBootstrap,
    TaskLifecycleMutation,
    TaskSeqAllocation,
    TaskSubtreeCascade,
    Transaction,
    WebChatSessionBootstrap,
)
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MigrationRunner,
    MigrationUnsupportedError,
    _split_statements_respecting_dollar_quotes,
)

_SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PRE_BASELINE_INFRA_TABLES: frozenset[str] = frozenset(
    {
        "gobby_install_ownership",
        "_pgaudit_probe",
    }
)
_BASELINE_BOOKKEEPING_TABLES: frozenset[str] = frozenset(
    {
        "schema_migrations",
        "gobby_migration_state",
    }
)
_PG_SEARCH_MISSING_MESSAGE = (
    "pg_search extension is not present on this database. Docker mode: rebuild the image. "
    "Native mode: rerun 'gobby postgres install --mode native'. External mode: install "
    "pg_search per docs/runbooks/postgres-pgsearch-install.md."
)
_BaselineState = Literal[
    "fresh",
    "fresh_with_install_infra",
    "already_baselined",
    "corrupt_partial",
]
_PLACEHOLDER_SCAN_CACHE = threading.local()
_BOOLEAN_COLUMNS: frozenset[str] = frozenset(
    {
        "allow_automation",
        "always_apply",
        "context_injected",
        "enabled",
        "floor_drift",
        "graph_processed",
        "graph_synced",
        "had_edits",
        "is_dev",
        "is_escalated",
        "is_high_value",
        "is_local",
        "is_secret",
        "is_system",
        "is_terminal",
        "pr_required",
        "reasoning_required",
        "remember_me",
        "requires_human",
        "sandbox_enabled",
        "success",
        "transcript_processed",
        "unattended",
        "vectors_synced",
        "webhook_enabled",
    }
)
_BOOLEAN_COLUMN_ALTERNATION = "|".join(sorted(_BOOLEAN_COLUMNS, key=len, reverse=True))
_BOOLEAN_LITERAL_RE = re.compile(
    rf"(?P<column>\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:{_BOOLEAN_COLUMN_ALTERNATION})\b)"
    r"\s*=\s*(?P<value>[01])\b"
)
_BOOLEAN_COALESCE_RE = re.compile(
    r"COALESCE\(\s*"
    rf"(?P<column>\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:{_BOOLEAN_COLUMN_ALTERNATION})\b)"
    r"\s*,\s*(?P<default>[01])\s*\)\s*=\s*(?P<value>[01])\b"
)
_BOOLEAN_PARAM_COMPARISON_RE = re.compile(
    rf"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:{_BOOLEAN_COLUMN_ALTERNATION})\b"
    r"\s*(?:=|IS)\s*%s\b"
)
_INSERT_VALUES_RE = re.compile(
    r"\bINSERT\s+INTO\s+(?:\"?[A-Za-z_][A-Za-z0-9_]*\"?\.)?\"?[A-Za-z_][A-Za-z0-9_]*\"?"
    r"\s*\((?P<columns>.*?)\)\s*VALUES\s*\((?P<values>.*?)\)",
    re.IGNORECASE | re.DOTALL,
)
_NULL_TEST_PARAM_RE = re.compile(r"%s\s+IS(?P<not>\s+NOT)?\s+NULL", re.IGNORECASE)


def _remap_placeholders_to_psycopg(
    sql: str,
    params: Sequence[Any],
) -> tuple[str, tuple[Any, ...]]:
    """Translate top-level hub placeholders to psycopg ``%s`` placeholders."""
    sql = _rewrite_sqlite_boolean_literals(sql)
    if params and "?" in sql and "$" not in sql:
        new_sql, new_params, indexes = remap_qmark_placeholders(sql, params, "%s")
    else:
        new_sql, new_params, indexes = remap_dollar_placeholders(sql, params, "%s")
    new_sql = _cast_null_test_placeholders(new_sql)
    new_params = _coerce_boolean_params(new_sql, new_params)
    _cache_param_permutation(sql, len(params), indexes)
    return new_sql, new_params


def _build_param_permutation(sql: str, param_count: int) -> list[int]:
    """Return output-position to input-position mapping for top-level ``$N`` params."""
    cached = _cached_param_permutation(sql, param_count)
    if cached is not None:
        return list(cached)
    _new_sql, indexes = _scan_placeholder_indexes(sql, param_count)
    return list(indexes)


def _remap_placeholders_to_psycopg_with_indexes(
    sql: str,
    params: Sequence[Any],
) -> tuple[str, tuple[Any, ...], tuple[int, ...]]:
    new_sql, new_params = _remap_placeholders_to_psycopg(sql, params)
    return new_sql, new_params, tuple(_build_param_permutation(sql, len(params)))


def _cache_param_permutation(sql: str, param_count: int, indexes: tuple[int, ...]) -> None:
    cache = cast(
        dict[tuple[str, int], tuple[int, ...]] | None,
        getattr(_PLACEHOLDER_SCAN_CACHE, "permutations", None),
    )
    if cache is None:
        cache = {}
        _PLACEHOLDER_SCAN_CACHE.permutations = cache
    cache[(sql, param_count)] = indexes


def _cached_param_permutation(sql: str, param_count: int) -> tuple[int, ...] | None:
    cache = cast(
        dict[tuple[str, int], tuple[int, ...]] | None,
        getattr(_PLACEHOLDER_SCAN_CACHE, "permutations", None),
    )
    if cache is None:
        return None
    return cache.get((sql, param_count))


def _scan_placeholder_indexes(sql: str, param_count: int) -> tuple[str, tuple[int, ...]]:
    if param_count and "?" in sql and "$" not in sql:
        return scan_qmark_placeholder_indexes(sql, param_count, "%s")
    return scan_dollar_placeholder_indexes(sql, param_count, "%s")


def _prepare_params(
    sql: str,
    params: Sequence[Any] | Mapping[str, Any],
) -> tuple[str, Sequence[Any] | Mapping[str, Any]]:
    if isinstance(params, Mapping):
        return _cast_null_test_placeholders(_rewrite_sqlite_boolean_literals(sql)), params
    return _remap_placeholders_to_psycopg(sql, params)


def _rewrite_sqlite_boolean_literals(sql: str) -> str:
    """Translate SQLite-style boolean integer predicates for Postgres boolean columns."""
    out: list[str] = []
    segment_start = 0
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] == "-" and i + 1 < n and sql[i + 1] == "-":
            i = _copy_rewritten_plain_segment(sql, segment_start, i, out)
            end = sql.find("\n", i)
            end = n if end < 0 else end
            out.append(sql[i:end])
            i = end
            segment_start = i
            continue
        if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
            i = _copy_rewritten_plain_segment(sql, segment_start, i, out)
            i = _copy_block_comment_segment(sql, i, out)
            segment_start = i
            continue
        if sql[i] == "'":
            i = _copy_rewritten_plain_segment(sql, segment_start, i, out)
            i = _copy_quoted_segment(sql, i, "'", out)
            segment_start = i
            continue
        if sql[i] == '"':
            i = _copy_rewritten_plain_segment(sql, segment_start, i, out)
            i = _copy_quoted_segment(sql, i, '"', out)
            segment_start = i
            continue
        if sql[i] == "$":
            end = _dollar_quote_end(sql, i)
            if end is not None:
                i = _copy_rewritten_plain_segment(sql, segment_start, i, out)
                out.append(sql[i:end])
                i = end
                segment_start = i
                continue
        i += 1

    _copy_rewritten_plain_segment(sql, segment_start, n, out)
    return "".join(out)


def _copy_rewritten_plain_segment(sql: str, start: int, end: int, out: list[str]) -> int:
    if end > start:
        out.append(_rewrite_boolean_literals_in_plain_sql(sql[start:end]))
    return end


def _rewrite_boolean_literals_in_plain_sql(sql: str) -> str:
    """Rewrite boolean integer predicates in a SQL segment with no strings/comments."""

    def replace_coalesce(match: re.Match[str]) -> str:
        default = "TRUE" if match.group("default") == "1" else "FALSE"
        value = "TRUE" if match.group("value") == "1" else "FALSE"
        return f"COALESCE({match.group('column')}, {default}) = {value}"

    def replace(match: re.Match[str]) -> str:
        literal = "TRUE" if match.group("value") == "1" else "FALSE"
        return f"{match.group('column')} = {literal}"

    return _BOOLEAN_LITERAL_RE.sub(replace, _BOOLEAN_COALESCE_RE.sub(replace_coalesce, sql))


def _copy_block_comment_segment(sql: str, start: int, out: list[str]) -> int:
    end = sql.find("*/", start + 2)
    end = len(sql) if end < 0 else end + 2
    out.append(sql[start:end])
    return end


def _copy_quoted_segment(sql: str, start: int, quote: str, out: list[str]) -> int:
    i = start + 1
    n = len(sql)
    while i < n:
        if sql[i] == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            i += 1
            out.append(sql[start:i])
            return i
        i += 1
    out.append(sql[start:n])
    return n


def _dollar_quote_end(sql: str, start: int) -> int | None:
    tag_end = start + 1
    n = len(sql)
    while tag_end < n and (sql[tag_end].isalnum() or sql[tag_end] == "_"):
        tag_end += 1
    if tag_end >= n or sql[tag_end] != "$":
        return None
    tag = sql[start : tag_end + 1]
    close = sql.find(tag, tag_end + 1)
    if close < 0:
        return None
    return close + len(tag)


def _cast_null_test_placeholders(sql: str) -> str:
    """Give bare ``%s IS NULL`` checks a type so Postgres can plan NULL params."""

    def replace(match: re.Match[str]) -> str:
        not_part = match.group("not") or ""
        return f"%s::text IS{not_part} NULL"

    return _NULL_TEST_PARAM_RE.sub(replace, sql)


def _coerce_boolean_params(sql: str, params: tuple[Any, ...]) -> tuple[Any, ...]:
    if not params:
        return params

    coerced = list(params)
    for index in _boolean_param_indexes(sql):
        if index < len(coerced):
            coerced[index] = _coerce_boolean_param(coerced[index])
    return tuple(coerced)


def _coerce_boolean_param(value: Any) -> Any:
    if type(value) is int and value in (0, 1):
        return bool(value)
    return value


def _boolean_param_indexes(sql: str) -> set[int]:
    indexes: set[int] = set()
    for match in _BOOLEAN_PARAM_COMPARISON_RE.finditer(sql):
        index = _placeholder_index_at(sql, match.end() - 2)
        if index is not None:
            indexes.add(index)
    indexes.update(_insert_boolean_param_indexes(sql))
    return indexes


def _placeholder_index_at(sql: str, placeholder_start: int) -> int | None:
    seen = 0
    for match in re.finditer(r"%s", sql):
        if match.start() == placeholder_start:
            return seen
        seen += 1
    return None


def _insert_boolean_param_indexes(sql: str) -> set[int]:
    indexes: set[int] = set()
    for match in _INSERT_VALUES_RE.finditer(sql):
        columns = _split_top_level_csv(match.group("columns"))
        values = _split_top_level_csv(match.group("values"))
        if len(columns) != len(values):
            continue
        value_start = match.start("values")
        offset = 0
        for column, value in zip(columns, values, strict=True):
            raw_column = _unquote_identifier(column)
            token = value.strip()
            token_start = sql.find(value, value_start + offset)
            if token_start >= 0:
                offset = token_start - value_start + len(value)
            if raw_column not in _BOOLEAN_COLUMNS or token != "%s" or token_start < 0:
                continue
            index = _placeholder_index_at(sql, token_start + value.index("%s"))
            if index is not None:
                indexes.add(index)
    return indexes


def _split_top_level_csv(text: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == quote:
                if i + 1 < len(text) and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            items.append(text[start:i].strip())
            start = i + 1
        i += 1
    items.append(text[start:].strip())
    return items


def _unquote_identifier(text: str) -> str:
    text = text.strip()
    if "." in text:
        text = text.rsplit(".", 1)[1]
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1].replace('""', '"')
    return text


class PostgresHubDatabase:
    """Hub database adapter backed by psycopg and PostgreSQL."""

    dialect: Literal["sqlite", "postgres"] = "postgres"

    def __init__(self, dsn: str) -> None:
        self._pool = ConnectionPool(
            conninfo=dsn,
            min_size=int(os.getenv("PGPOOL_MIN", "2")),
            max_size=int(os.getenv("PGPOOL_MAX", "10")),
            timeout=int(os.getenv("PGCONNECT_TIMEOUT", "5")),
            kwargs={
                "application_name": os.getenv("PGAPPNAME", "gobby"),
                "row_factory": dict_row,
            },
        )
        self._state = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with enter_transaction(self, self._native_transaction) as txn:
            yield txn

    @contextmanager
    def transaction_immediate(self, lock: LockTarget | None = None) -> Iterator[Transaction]:
        with enter_transaction(self, self._native_transaction, immediate=True, lock=lock) as txn:
            yield txn

    @contextmanager
    def _native_transaction(
        self,
        *,
        immediate: bool,
        lock: LockTarget | None,
    ) -> Iterator[Transaction]:
        with self._transaction_context(is_immediate=immediate, initial_lock=lock) as txn:
            yield txn

    @contextmanager
    def _transaction_context(
        self,
        *,
        is_immediate: bool,
        initial_lock: LockTarget | None = None,
    ) -> Iterator[Transaction]:
        start_len = _lock_stack_len(self._state)
        try:
            if initial_lock is not None:
                _acquire_lock(self._state, initial_lock)
            callbacks: list[Callable[[], Any]] = []
            _push_after_commit_scope(self._state)
            try:
                with self._pool.connection() as conn, conn.transaction():
                    txn = _PostgresTransaction(
                        conn,
                        is_immediate=is_immediate,
                        state=self._state,
                    )
                    if initial_lock is not None:
                        txn._acquire_lock_target(initial_lock)
                    yield txn
                callbacks = _pop_after_commit_scope(self._state, committed=True)
            except Exception:
                _pop_after_commit_scope(self._state, committed=False)
                raise

            for callback in callbacks:
                callback()
        finally:
            _truncate_lock_stack(self._state, start_len)

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        ambient = ambient_transaction(self)
        if ambient is not None:
            return ambient.execute(sql, params)
        with self.transaction() as txn:
            return txn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Cursor:
        ambient = ambient_transaction(self)
        if ambient is not None:
            return ambient.executemany(sql, rows)
        with self.transaction() as txn:
            return txn.executemany(sql, rows)

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Row | None:
        with self.transaction() as txn:
            return txn.execute(sql, params).fetchone()

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[Row]:
        with self.transaction() as txn:
            return txn.execute(sql, params).fetchall()

    def safe_update(
        self,
        table: str,
        values: Mapping[str, Any],
        where: str,
        where_params: Sequence[Any] = (),
    ) -> Cursor:
        built = _build_safe_update(table, values, where, where_params)
        if built is None:
            return _PostgresCursor(None, rowcount=0)
        sql, params = built
        return self.execute(sql, params)

    def apply_migrations(self) -> None:
        runner = MigrationRunner(self)
        if not self._postgres_baseline_already_applied():
            self._apply_postgres_baseline()
        runner.apply_pending()

    def _postgres_baseline_already_applied(self) -> bool:
        with self._pool.connection() as conn:
            return _classify_baseline_state(conn) == "already_baselined"

    def _apply_postgres_baseline(self) -> None:
        with self._pool.connection() as fast_conn:
            if _classify_baseline_state(fast_conn) == "already_baselined":
                return

        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('postgres_baseline_apply'))")
            state = _classify_baseline_state(conn)
            if state == "already_baselined":
                return
            if state == "corrupt_partial":
                raise MigrationUnsupportedError(
                    "Postgres database has application tables but no schema_migrations; "
                    "dump-and-restore from a known-good baseline."
                )
            _require_pg_search_extension(conn)

            sql = (
                importlib.resources.files("gobby.storage")
                .joinpath("postgres_baseline_schema.sql")
                .read_text()
            )
            for statement in _split_statements_respecting_dollar_quotes(sql):
                if statement.strip():
                    conn.execute(statement)
            sql, params = _remap_placeholders_to_psycopg(
                "INSERT INTO schema_migrations (version, applied_at) VALUES ($1, NOW())",
                (BASELINE_VERSION,),
            )
            conn.execute(sql, params)

    def close(self) -> None:
        self._pool.close()


class _PostgresTransaction:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        is_immediate: bool = False,
        state: threading.local | None = None,
    ) -> None:
        self._conn = conn
        self.is_immediate = is_immediate
        self._state = state if state is not None else threading.local()

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        new_sql, new_params = _prepare_params(sql, params)
        result = (
            self._conn.execute(new_sql, new_params) if new_params else self._conn.execute(new_sql)
        )
        return _PostgresCursor(result)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Cursor:
        materialized = [tuple(row) for row in rows]
        if not materialized:
            return _PostgresCursor(None, rowcount=0)

        first = materialized[0]
        new_sql, first_permuted, permutation = _remap_placeholders_to_psycopg_with_indexes(
            sql,
            first,
        )
        permuted_rows = [first_permuted]
        permuted_rows.extend(
            _coerce_boolean_params(new_sql, _params_from_indexes(row, permutation))
            for row in materialized[1:]
        )
        driver_executemany = getattr(self._conn, "executemany", None)
        if callable(driver_executemany):
            driver_executemany(new_sql, permuted_rows)
            return _PostgresCursor(None)
        with self._conn.cursor() as cursor:
            cursor.executemany(new_sql, permuted_rows)
            return _PostgresCursor(None, rowcount=cursor.rowcount)

    def savepoint(self, name: str) -> Savepoint:
        quoted_name = _quote_identifier(name)
        self._conn.execute(f"SAVEPOINT {quoted_name}")
        return _PostgresSavepoint(self._conn, quoted_name)

    def after_commit(self, callback: Callable[[], None]) -> None:
        _after_commit(self._state, callback)

    def acquire_additional_lock(self, lock: LockTarget) -> None:
        if not self.is_immediate:
            raise RuntimeError("additional locks require an immediate transaction")

        start_len = _lock_stack_len(self._state)
        _acquire_lock(self._state, lock)
        try:
            self._acquire_lock_target(lock)
        except Exception:
            _truncate_lock_stack(self._state, start_len)
            raise

    def _acquire_lock_target(self, lock: LockTarget) -> None:
        if isinstance(lock, TaskSeqAllocation):
            row = self.execute(
                "SELECT 1 FROM projects WHERE id = $1 FOR UPDATE",
                (lock.project_id,),
            ).fetchone()
            if row is not None:
                return
            self._acquire_advisory_lock(f"task_seq:{lock.project_id}")
            return

        for lock_key in _advisory_lock_keys(lock):
            self._acquire_advisory_lock(lock_key)

    def _acquire_advisory_lock(self, lock_key: str) -> None:
        self.execute("SELECT pg_advisory_xact_lock(hashtext($1))", (lock_key,))


class _PostgresCursor:
    def __init__(self, cursor: Any | None, *, rowcount: int = -1) -> None:
        self._cursor = cursor
        self._rowcount = rowcount

    def fetchone(self) -> Row | None:
        if self._cursor is None:
            return None
        return _normalize_row(cast(Row | None, self._cursor.fetchone()))

    def fetchall(self) -> list[Row]:
        if self._cursor is None:
            return []
        return [
            row
            for row in (_normalize_row(row) for row in cast(Sequence[Row], self._cursor.fetchall()))
            if row is not None
        ]

    @property
    def rowcount(self) -> int:
        if self._cursor is None:
            return self._rowcount
        return int(getattr(self._cursor, "rowcount", self._rowcount))

    @property
    def lastrowid(self) -> int | None:
        return None


def _normalize_row(row: Row | None) -> Row | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return cast(Row, {str(key): _normalize_value(value) for key, value in row.items()})
    return row


def _normalize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


class _PostgresSavepoint:
    def __init__(self, conn: psycopg.Connection[Any], quoted_name: str) -> None:
        self._conn = conn
        self._quoted_name = quoted_name

    def release(self) -> None:
        self._conn.execute(f"RELEASE SAVEPOINT {self._quoted_name}")

    def rollback(self) -> None:
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {self._quoted_name}")


def _classify_baseline_state(conn: Any) -> _BaselineState:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    ).fetchall()
    tables = {str(_row_value(row, "tablename")) for row in rows}
    has_bookkeeping = "schema_migrations" in tables
    application_tables = tables - _PRE_BASELINE_INFRA_TABLES - _BASELINE_BOOKKEEPING_TABLES

    if has_bookkeeping and _has_baseline_version(conn, BASELINE_VERSION):
        return "already_baselined"
    if has_bookkeeping and not application_tables:
        return "fresh"
    if not has_bookkeeping and not application_tables:
        if tables & _PRE_BASELINE_INFRA_TABLES:
            return "fresh_with_install_infra"
        return "fresh"
    return "corrupt_partial"


def _has_baseline_version(conn: Any, version: int) -> bool:
    sql, params = _remap_placeholders_to_psycopg(
        "SELECT 1 FROM schema_migrations WHERE version = $1 LIMIT 1",
        (version,),
    )
    row = conn.execute(
        sql,
        params,
    ).fetchone()
    return row is not None


def _require_pg_search_extension(conn: Any) -> None:
    row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_search'").fetchone()
    if row is None:
        raise MigrationUnsupportedError(_PG_SEARCH_MISSING_MESSAGE)


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]


def _quote_identifier(identifier: str) -> str:
    if not _SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _build_safe_update(
    table: str,
    values: Mapping[str, Any],
    where: str,
    where_params: Sequence[Any],
) -> tuple[str, tuple[Any, ...]] | None:
    if not values:
        return None
    _validate_identifier(table)

    update_params: list[Any] = []
    set_clauses: list[str] = []
    if "?" in where and "$" not in where:
        for column, value in values.items():
            _validate_identifier(column)
            set_clauses.append(f"{column} = ?")
            update_params.append(value)

        sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {where}"  # nosec B608
        return sql, (*update_params, *where_params)

    for index, (column, value) in enumerate(values.items(), start=1):
        _validate_identifier(column)
        set_clauses.append(f"{column} = ${index}")
        update_params.append(value)

    final_where = _shift_dollar_placeholders(where, len(update_params))
    sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {final_where}"  # nosec B608
    return sql, (*update_params, *where_params)


def _validate_identifier(identifier: str) -> None:
    if not _SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier: {identifier!r}")


def _shift_dollar_placeholders(sql: str, offset: int) -> str:
    return re.sub(r"\$(\d+)", lambda match: f"${int(match.group(1)) + offset}", sql)


def _advisory_lock_keys(lock: LockTarget) -> tuple[str, ...]:
    if isinstance(lock, DispatchMutexRow):
        return (f"dispatch_mutex:{lock.task_id}",)
    if isinstance(lock, SessionRegistration):
        return (
            "session_register:"
            f"{lock.external_id}|{lock.machine_id}|{lock.source}|"
            f"{lock.project_id or ''}|{lock.session_type}",
        )
    if isinstance(lock, SessionRecoveryByProject):
        return (f"session_recovery:{lock.project_id}",)
    if isinstance(lock, WebChatSessionBootstrap):
        return (
            "web_chat_session:"
            f"{lock.external_id}|{lock.machine_id}|{lock.source}|"
            f"{lock.project_id or ''}|{lock.session_type}",
        )
    if isinstance(lock, TaskSubtreeCascade):
        return (f"task_subtree_cascade:{lock.project_id}",)
    if isinstance(lock, SystemSessionBootstrap):
        return ("system_session_bootstrap",)
    if isinstance(lock, TaskLifecycleMutation):
        return (f"task_lifecycle:{lock.task_id}",)
    if isinstance(lock, ChatAttachmentMutation):
        return ("chat_attachment_mutation",)

    lock_type = type(lock)
    return (f"{lock_type.__module__}.{lock_type.__qualname__}:{lock}",)


def _lock_stack(state: threading.local) -> list[LockTarget]:
    stack = getattr(state, "lock_stack", None)
    if stack is None:
        stack = []
        state.lock_stack = stack
    return cast(list[LockTarget], stack)


def _lock_stack_len(state: threading.local) -> int:
    stack = getattr(state, "lock_stack", None)
    if stack is None:
        return 0
    return len(cast(list[LockTarget], stack))


def _truncate_lock_stack(state: threading.local, length: int) -> None:
    stack = _lock_stack(state)
    del stack[length:]


def _acquire_lock(state: threading.local, lock: LockTarget) -> None:
    stack = _lock_stack(state)
    if stack:
        current = stack[-1]
        if lock.PRIORITY <= current.PRIORITY:
            raise LockAcquisitionOrderError(
                "nested lock priority must increase: "
                f"{current.PRIORITY} ({current}) -> {lock.PRIORITY} ({lock})"
            )
    stack.append(lock)


def _after_commit(state: threading.local, callback: Callable[[], Any]) -> None:
    stack = getattr(state, "after_commit_stack", None)
    if not stack:
        callback()
        return
    cast(list[list[Callable[[], Any]]], stack)[-1].append(callback)


def _push_after_commit_scope(state: threading.local) -> None:
    stack = cast(
        list[list[Callable[[], Any]]] | None,
        getattr(state, "after_commit_stack", None),
    )
    if stack is None:
        stack = []
        state.after_commit_stack = stack
    stack.append([])


def _pop_after_commit_scope(
    state: threading.local,
    *,
    committed: bool,
) -> list[Callable[[], Any]]:
    stack = cast(
        list[list[Callable[[], Any]]] | None,
        getattr(state, "after_commit_stack", None),
    )
    if not stack:
        return []

    callbacks = stack.pop()
    if committed and stack:
        stack[-1].extend(callbacks)
        callbacks = []
    if not stack:
        state.after_commit_stack = []
    return callbacks
