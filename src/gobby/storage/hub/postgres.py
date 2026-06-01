"""PostgreSQL implementation of the hub database protocol."""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Literal, cast

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gobby.storage.hub._ambient import ambient_transaction, enter_transaction
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
    }
)
_GCORE_CODE_INDEX_TABLES: frozenset[str] = frozenset(
    {
        "code_indexed_projects",
        "code_indexed_files",
        "code_symbols",
        "code_imports",
        "code_calls",
        "code_content_chunks",
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
    "gcore_code_index",
    "gwiki_standalone",
    "already_baselined",
    "corrupt_partial",
]


class PostgresHubDatabase:
    """Hub database adapter backed by psycopg and PostgreSQL."""

    dialect: Literal["postgres"] = "postgres"

    def __init__(self, dsn: str) -> None:
        self._pool = ConnectionPool(
            conninfo=dsn,
            open=False,
            min_size=int(os.getenv("PGPOOL_MIN", "2")),
            max_size=int(os.getenv("PGPOOL_MAX", "10")),
            timeout=int(os.getenv("PGCONNECT_TIMEOUT", "5")),
            kwargs={
                "application_name": os.getenv("PGAPPNAME", "gobby"),
                "row_factory": dict_row,
            },
        )
        self._state = threading.local()
        self._open_lock = threading.Lock()
        self._pool_opened = False

    def open(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Open the lazy connection pool before first use."""
        open_pool = getattr(self._pool, "open", None)
        if not callable(open_pool):
            return

        with self._open_lock:
            if getattr(self, "_pool_opened", False):
                return
            open_timeout = timeout
            if open_timeout is None:
                open_timeout = float(os.getenv("PGPOOL_OPEN_TIMEOUT", "30"))
            open_pool(wait=wait, timeout=open_timeout)
            self._pool_opened = True

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
        self.open()
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
        self.open()
        with self._pool.connection() as conn:
            return _classify_baseline_state(conn) == "already_baselined"

    def _apply_postgres_baseline(self) -> None:
        self.open()
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
            for statement in _baseline_statements_for_state(sql, state):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, NOW())",
                (BASELINE_VERSION,),
            )

    def close(self) -> None:
        self._pool.close()
        self._pool_opened = False


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
        result = self._conn.execute(sql, params) if params else self._conn.execute(sql)
        return _PostgresCursor(result)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Cursor:
        materialized = [tuple(row) for row in rows]
        if not materialized:
            return _PostgresCursor(None, rowcount=0)

        driver_executemany = getattr(self._conn, "executemany", None)
        if callable(driver_executemany):
            driver_executemany(sql, materialized)
            return _PostgresCursor(None)
        with self._conn.cursor() as cursor:
            cursor.executemany(sql, materialized)
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
                "SELECT 1 FROM projects WHERE id = %s FOR UPDATE",
                (lock.project_id,),
            ).fetchone()
            if row is not None:
                return
            self._acquire_advisory_lock(f"task_seq:{lock.project_id}")
            return

        for lock_key in _advisory_lock_keys(lock):
            self._acquire_advisory_lock(lock_key)

    def _acquire_advisory_lock(self, lock_key: str) -> None:
        self.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))


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
    if isinstance(value, date):
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
    if (
        not has_bookkeeping
        and _GCORE_CODE_INDEX_TABLES.issubset(application_tables)
        and all(
            table in _GCORE_CODE_INDEX_TABLES or _is_gwiki_table(table)
            for table in application_tables
        )
    ):
        return "gcore_code_index"
    if (
        not has_bookkeeping
        and application_tables
        and all(_is_gwiki_table(table) for table in application_tables)
    ):
        return "gwiki_standalone"
    return "corrupt_partial"


def _baseline_statements_for_state(sql: str, state: _BaselineState) -> Iterator[str]:
    statements = _split_statements_respecting_dollar_quotes(sql)
    if state not in ("gcore_code_index", "gwiki_standalone"):
        yield from statements
        return

    for statement in statements:
        if state == "gcore_code_index" and _is_code_index_create_statement(statement):
            continue
        if _is_gwiki_create_statement(statement):
            continue
        yield statement


def _is_code_index_create_statement(statement: str) -> bool:
    return _is_create_statement_for_table(
        statement, lambda table: table in _GCORE_CODE_INDEX_TABLES
    )


def _is_gwiki_create_statement(statement: str) -> bool:
    return _is_create_statement_for_table(statement, _is_gwiki_table)


def _is_create_statement_for_table(
    statement: str,
    table_matches: Callable[[str], bool],
) -> bool:
    text = statement.strip()
    table_match = re.match(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
        text,
        re.IGNORECASE,
    )
    if table_match:
        return table_matches(table_match.group(1))

    index_match = re.match(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"\"?[A-Za-z_][A-Za-z0-9_]*\"?"
        r"\s+ON\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if index_match:
        return table_matches(index_match.group(1))

    return False


def _is_gwiki_table(table: str) -> bool:
    return table.startswith("gwiki_")


def _has_baseline_version(conn: Any, version: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = %s LIMIT 1",
        (version,),
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
    for column, value in values.items():
        _validate_identifier(column)
        set_clauses.append(f"{column} = %s")
        update_params.append(value)

    sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {where}"  # nosec B608
    return sql, (*update_params, *where_params)


def _validate_identifier(identifier: str) -> None:
    if not _SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier: {identifier!r}")


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
