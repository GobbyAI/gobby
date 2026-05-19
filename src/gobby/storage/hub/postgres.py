"""PostgreSQL implementation of the hub database protocol."""

from __future__ import annotations

import importlib.resources
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Literal, cast

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gobby.storage.hub.protocol import (
    Cursor,
    LockAcquisitionOrderError,
    LockTarget,
    Savepoint,
    Transaction,
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
_BaselineState = Literal[
    "fresh",
    "fresh_with_install_infra",
    "already_baselined",
    "corrupt_partial",
]
_PLACEHOLDER_SCAN_CACHE = threading.local()


def _remap_placeholders_to_psycopg(
    sql: str,
    params: Sequence[Any],
) -> tuple[str, tuple[Any, ...]]:
    """Translate top-level ``$N`` placeholders to psycopg ``%s`` placeholders."""
    new_sql, indexes = _scan_placeholder_indexes(sql, len(params))
    _cache_param_permutation(sql, len(params), indexes)
    return new_sql, _params_from_indexes(params, indexes)


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
    out: list[str] = []
    indexes: list[int] = []
    i = 0
    n = len(sql)

    while i < n:
        char = sql[i]

        if char == "-" and i + 1 < n and sql[i + 1] == "-":
            end = sql.find("\n", i)
            end = n if end < 0 else end
            out.append(sql[i:end])
            i = end
            continue

        if char == "/" and i + 1 < n and sql[i + 1] == "*":
            i = _copy_block_comment(sql, i, out)
            continue

        if char == "'":
            i = _copy_single_quoted_string(sql, i, out)
            continue

        if char == '"':
            i = _copy_double_quoted_identifier(sql, i, out)
            continue

        if char == "$":
            remapped = _try_remap_dollar_token(sql, i, param_count, out, indexes)
            if remapped is not None:
                i = remapped
                continue

        out.append(char)
        i += 1

    return "".join(out), tuple(indexes)


def _copy_block_comment(sql: str, start: int, out: list[str]) -> int:
    i = start + 2
    depth = 1
    n = len(sql)
    while i < n and depth:
        if i + 1 < n and sql[i] == "/" and sql[i + 1] == "*":
            depth += 1
            i += 2
            continue
        if i + 1 < n and sql[i] == "*" and sql[i + 1] == "/":
            depth -= 1
            i += 2
            continue
        i += 1
    out.append(sql[start:i])
    return i


def _copy_single_quoted_string(sql: str, start: int, out: list[str]) -> int:
    i = start
    n = len(sql)
    out.append(sql[i])
    i += 1

    while i < n:
        if sql[i] == "'":
            if i + 1 < n and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            out.append("'")
            return i + 1
        out.append(sql[i])
        i += 1

    return i


def _copy_double_quoted_identifier(sql: str, start: int, out: list[str]) -> int:
    i = start
    n = len(sql)
    out.append(sql[i])
    i += 1

    while i < n:
        if sql[i] == '"':
            if i + 1 < n and sql[i + 1] == '"':
                out.append('""')
                i += 2
                continue
            out.append('"')
            return i + 1
        out.append(sql[i])
        i += 1

    return i


def _try_remap_dollar_token(
    sql: str,
    start: int,
    param_count: int,
    out: list[str],
    indexes: list[int],
) -> int | None:
    if start > 0 and _is_identifier_continuation(sql[start - 1]):
        return None

    tag_end = start + 1
    n = len(sql)
    while tag_end < n and _is_identifier_continuation(sql[tag_end]):
        tag_end += 1

    if tag_end < n and sql[tag_end] == "$":
        tag = sql[start : tag_end + 1]
        close = sql.find(tag, tag_end + 1)
        if close < 0:
            raise ValueError(f"unterminated dollar-quote tag {tag!r}")
        end = close + len(tag)
        out.append(sql[start:end])
        return end

    digits = sql[start + 1 : tag_end]
    if digits and digits.isdigit():
        index = int(digits)
        if index < 1 or index > param_count:
            raise ValueError(
                f"placeholder ${index} has no matching param "
                f"(query references {param_count} params total)"
            )
        out.append("%s")
        indexes.append(index - 1)
        return tag_end

    return None


def _is_identifier_continuation(char: str) -> bool:
    return char.isalnum() or char == "_"


def _params_from_indexes(params: Sequence[Any], indexes: Sequence[int]) -> tuple[Any, ...]:
    remapped: list[Any] = []
    for index in indexes:
        if index >= len(params):
            raise ValueError(
                f"placeholder ${index + 1} has no matching param "
                f"(query references {len(params)} params total)"
            )
        remapped.append(params[index])
    return tuple(remapped)


def _prepare_params(
    sql: str,
    params: Sequence[Any] | Mapping[str, Any],
) -> tuple[str, Sequence[Any] | Mapping[str, Any]]:
    if isinstance(params, Mapping):
        return sql, params
    return _remap_placeholders_to_psycopg(sql, params)


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
        with self._transaction_context(is_immediate=False) as txn:
            yield txn

    @contextmanager
    def transaction_immediate(self, lock: LockTarget) -> Iterator[Transaction]:
        start_len = _lock_stack_len(self._state)
        _acquire_lock(self._state, lock)
        try:
            with self._transaction_context(is_immediate=True, initial_lock=lock) as txn:
                yield txn
        finally:
            _truncate_lock_stack(self._state, start_len)

    @contextmanager
    def _transaction_context(
        self,
        *,
        is_immediate: bool,
        initial_lock: LockTarget | None = None,
    ) -> Iterator[Transaction]:
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
                    txn._acquire_advisory_lock(initial_lock)
                yield txn
            callbacks = _pop_after_commit_scope(self._state, committed=True)
        except Exception:
            _pop_after_commit_scope(self._state, committed=False)
            raise

        for callback in callbacks:
            callback()

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

            sql = (
                importlib.resources.files("gobby.storage")
                .joinpath("postgres_baseline_schema.sql")
                .read_text()
            )
            for statement in _split_statements_respecting_dollar_quotes(sql):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, NOW())",
                (BASELINE_VERSION,),
            )

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
        return cast(Cursor, result)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        materialized = [tuple(row) for row in rows]
        if not materialized:
            return

        first = materialized[0]
        new_sql, first_permuted, permutation = _remap_placeholders_to_psycopg_with_indexes(
            sql,
            first,
        )
        permuted_rows = [first_permuted]
        permuted_rows.extend(_params_from_indexes(row, permutation) for row in materialized[1:])
        driver_executemany = getattr(self._conn, "executemany", None)
        if callable(driver_executemany):
            driver_executemany(new_sql, permuted_rows)
            return
        with self._conn.cursor() as cursor:
            cursor.executemany(new_sql, permuted_rows)

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
            self._acquire_advisory_lock(lock)
        except Exception:
            _truncate_lock_stack(self._state, start_len)
            raise

    def _acquire_advisory_lock(self, lock: LockTarget) -> None:
        lock_key = _advisory_lock_key(lock)
        self.execute("SELECT pg_advisory_xact_lock(hashtext($1))", (lock_key,))


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
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = %s LIMIT 1",
        (version,),
    ).fetchone()
    return row is not None


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]


def _quote_identifier(identifier: str) -> str:
    if not _SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _advisory_lock_key(lock: LockTarget) -> str:
    lock_type = type(lock)
    return f"{lock_type.__module__}.{lock_type.__qualname__}:{lock}"


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
