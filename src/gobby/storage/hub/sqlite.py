"""SQLite implementation of the hub database protocol."""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Literal, cast

from gobby.storage import migrations as _migrations
from gobby.storage.database import LocalDatabase
from gobby.storage.hub._ambient import ambient_transaction, enter_transaction
from gobby.storage.hub.placeholders import (
    params_from_indexes as _params_from_indexes,
    remap_dollar_placeholders,
    scan_dollar_placeholder_indexes,
)
from gobby.storage.hub.protocol import (
    Cursor,
    LockAcquisitionOrderError,
    LockTarget,
    Row,
    Savepoint,
    Transaction,
)

MigrationRunner = getattr(_migrations, "MigrationRunner", None)

_SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _remap_placeholders(sql: str, params: Sequence[Any]) -> tuple[str, tuple[Any, ...]]:
    """Translate top-level ``$N`` placeholders to SQLite ``?`` placeholders.

    The scanner copies SQL strings, comments, and Postgres dollar-quoted bodies
    verbatim. Only top-level ``$N`` placeholders are rewritten, with params
    reordered or repeated according to the ordinal used in SQL.
    """
    new_sql, new_params, _indexes = remap_dollar_placeholders(sql, params, "?")
    return new_sql, new_params


def _scan_placeholder_indexes(sql: str, param_count: int) -> tuple[str, tuple[int, ...]]:
    return scan_dollar_placeholder_indexes(sql, param_count, "?")


def _prepare_params(
    sql: str,
    params: Sequence[Any] | Mapping[str, Any],
) -> tuple[str, Sequence[Any] | Mapping[str, Any]]:
    if isinstance(params, Mapping):
        return sql, params
    if "?" in sql:
        return sql, params
    return _remap_placeholders(sql, params)


def _row_to_dict(row: sqlite3.Row | None) -> Row | None:
    if row is None:
        return None
    return _sqlite_row_to_dict(row)


def _sqlite_row_to_dict(row: sqlite3.Row) -> Row:
    return dict(row)


class SqliteHubDatabase:
    """Hub database adapter backed by the existing local SQLite stack."""

    dialect: Literal["sqlite", "postgres"] = "sqlite"

    def __init__(self, path: str) -> None:
        self._local = LocalDatabase(path)
        self._lock_state = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with enter_transaction(self, self._native_transaction) as txn:
            yield txn

    @contextmanager
    def transaction_immediate(self, lock: LockTarget) -> Iterator[Transaction]:
        with enter_transaction(self, self._native_transaction, immediate=True, lock=lock) as txn:
            yield txn

    @contextmanager
    def _native_transaction(
        self,
        *,
        immediate: bool,
        lock: LockTarget | None,
    ) -> Iterator[Transaction]:
        if immediate:
            if lock is None:
                raise TypeError("transaction_immediate() requires a LockTarget")
            with self._native_immediate_transaction(lock) as txn:
                yield txn
            return

        with self._local.transaction() as conn:
            yield _SqliteTransaction(
                self._local,
                conn,
                is_immediate=False,
                lock_state=self._lock_state,
            )

    @contextmanager
    def _native_immediate_transaction(self, lock: LockTarget) -> Iterator[Transaction]:
        start_len = _lock_stack_len(self._lock_state)
        _acquire_lock(self._lock_state, lock)
        try:
            with self._local.transaction_immediate() as conn:
                yield _SqliteTransaction(
                    self._local,
                    conn,
                    is_immediate=True,
                    lock_state=self._lock_state,
                )
        finally:
            _truncate_lock_stack(self._lock_state, start_len)

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
            return _NoOpCursor()
        sql, params = built
        return self.execute(sql, params)

    def apply_migrations(self) -> None:
        if MigrationRunner is not None:
            MigrationRunner(self).apply_pending()
            _migrations._run_sqlite_startup_repairs(self._local)
            return
        _migrations.run_migrations(self._local)

    def close(self) -> None:
        self._local.close()


class _SqliteTransaction:
    def __init__(
        self,
        local: LocalDatabase,
        conn: sqlite3.Connection,
        *,
        is_immediate: bool,
        lock_state: threading.local,
    ) -> None:
        self._local = local
        self._conn = conn
        self.is_immediate = is_immediate
        self._lock_state = lock_state

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        new_sql, new_params = _prepare_params(sql, params)
        cursor = self._conn.execute(new_sql, new_params)
        return _SqliteCursor(cursor)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Cursor:
        materialized = [tuple(row) for row in rows]
        if not materialized:
            return _NoOpCursor()
        if "?" in sql:
            cursor = self._conn.executemany(sql, materialized)
            return _SqliteCursor(cursor)
        new_sql, indexes = _scan_placeholder_indexes(sql, len(materialized[0]))
        remapped_rows = [_params_from_indexes(row, indexes) for row in materialized]
        cursor = self._conn.executemany(new_sql, remapped_rows)
        return _SqliteCursor(cursor)

    def savepoint(self, name: str) -> Savepoint:
        quoted_name = _quote_identifier(name)
        self._conn.execute(f"SAVEPOINT {quoted_name}")
        return _SqliteSavepoint(self._conn, quoted_name)

    def after_commit(self, callback: Callable[[], None]) -> None:
        self._local.after_commit(callback)

    def acquire_additional_lock(self, lock: LockTarget) -> None:
        if not self.is_immediate:
            raise RuntimeError("additional locks require an immediate transaction")
        _acquire_lock(self._lock_state, lock)


class _SqliteCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def fetchone(self) -> Row | None:
        row = cast(sqlite3.Row | None, self._cursor.fetchone())
        return _row_to_dict(row)

    def fetchall(self) -> list[Row]:
        rows = cast(Sequence[sqlite3.Row], self._cursor.fetchall())
        return [_sqlite_row_to_dict(row) for row in rows]

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        value = self._cursor.lastrowid
        return int(value) if isinstance(value, int) else None


class _NoOpCursor:
    def fetchone(self) -> Row | None:
        return None

    def fetchall(self) -> list[Row]:
        return []

    @property
    def rowcount(self) -> int:
        return 0

    @property
    def lastrowid(self) -> int | None:
        return None


class _SqliteSavepoint:
    def __init__(self, conn: sqlite3.Connection, quoted_name: str) -> None:
        self._conn = conn
        self._quoted_name = quoted_name

    def release(self) -> None:
        self._conn.execute(f"RELEASE SAVEPOINT {self._quoted_name}")

    def rollback(self) -> None:
        self._conn.execute(f"ROLLBACK TO SAVEPOINT {self._quoted_name}")


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
    if "?" in where:
        placeholder = "?"
        for column, value in values.items():
            _validate_identifier(column)
            set_clauses.append(f"{column} = {placeholder}")
            update_params.append(value)
        final_where = where
    else:
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


def _lock_stack(lock_state: threading.local) -> list[LockTarget]:
    stack = getattr(lock_state, "stack", None)
    if stack is None:
        stack = []
        lock_state.stack = stack
    return cast(list[LockTarget], stack)


def _lock_stack_len(lock_state: threading.local) -> int:
    stack = getattr(lock_state, "stack", None)
    if stack is None:
        return 0
    return len(cast(list[LockTarget], stack))


def _truncate_lock_stack(lock_state: threading.local, length: int) -> None:
    stack = _lock_stack(lock_state)
    del stack[length:]


def _acquire_lock(lock_state: threading.local, lock: LockTarget) -> None:
    stack = _lock_stack(lock_state)
    if stack:
        current = stack[-1]
        if lock.PRIORITY <= current.PRIORITY:
            raise LockAcquisitionOrderError(
                "nested lock priority must increase: "
                f"{current.PRIORITY} ({current}) -> {lock.PRIORITY} ({lock})"
            )
    stack.append(lock)
