"""PostgreSQL implementation of the hub database protocol."""

from __future__ import annotations

import asyncio
import atexit
import importlib.resources
import json
import logging
import os
import re
import threading
import uuid
import weakref
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, asynccontextmanager, contextmanager
from datetime import date, datetime
from typing import Any, Literal, cast

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from gobby.config.postgres_pool import DEFAULT_POSTGRES_POOL_CONFIG, PostgresPoolConfig
from gobby.storage.hub._ambient import ambient_transaction, enter_transaction
from gobby.storage.hub.protocol import (
    AgentCapAdmission,
    BuildDryRunMutation,
    ChatAttachmentMutation,
    CronRunAdmission,
    Cursor,
    DispatchMutexRow,
    GitHubIssueTriageMutation,
    IntegrationWorkspaceMutex,
    LockAcquisitionOrderError,
    LockTarget,
    Row,
    Savepoint,
    SessionRecoveryByProject,
    SessionRegistration,
    SessionSeqMutation,
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
from gobby.utils.datetime import to_aware_utc, to_json_safe

logger = logging.getLogger(__name__)

_OPEN_DATABASES: weakref.WeakSet[PostgresHubDatabase] = weakref.WeakSet()
_POOL_CLOSE_TIMEOUT_SECONDS = 2.0


def _close_open_databases_at_exit() -> None:
    """Close any hub pools still open when the process exits.

    atexit runs before interpreter finalization, where joining the pool's
    worker threads is still legal. A pool that instead reaches GC during
    finalization raises PythonFinalizationError from ConnectionPool.__del__
    on Python 3.14, spraying tracebacks on stderr at CLI exit.
    """
    for db in list(_OPEN_DATABASES):
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close PostgreSQL hub pool at exit", exc_info=True)


atexit.register(_close_open_databases_at_exit)

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
_GCORE_CODE_INDEX_COLUMNS: Mapping[str, frozenset[str]] = {
    "code_indexed_projects": frozenset(
        {
            "id",
            "root_path",
            "total_files",
            "total_symbols",
            "last_indexed_at",
            "index_duration_ms",
            "created_at",
            "updated_at",
        }
    ),
    "code_indexed_files": frozenset(
        {
            "id",
            "project_id",
            "file_path",
            "language",
            "content_hash",
            "symbol_count",
            "byte_size",
            "graph_synced",
            "vectors_synced",
            "graph_sync_attempted_at",
            "vector_sync_attempted_at",
            "indexed_at",
        }
    ),
    "code_symbols": frozenset(
        {
            "id",
            "project_id",
            "file_path",
            "name",
            "qualified_name",
            "kind",
            "language",
            "byte_start",
            "byte_end",
            "line_start",
            "line_end",
            "signature",
            "docstring",
            "parent_symbol_id",
            "content_hash",
            "summary",
            "summary_attempted_at",
            "created_at",
            "updated_at",
        }
    ),
    "code_imports": frozenset({"id", "project_id", "source_file", "target_module"}),
    "code_calls": frozenset(
        {
            "id",
            "project_id",
            "caller_symbol_id",
            "callee_symbol_id",
            "callee_name",
            "callee_target_kind",
            "callee_external_module",
            "file_path",
            "line",
        }
    ),
    "code_content_chunks": frozenset(
        {
            "id",
            "project_id",
            "file_path",
            "chunk_index",
            "line_start",
            "line_end",
            "content",
            "language",
            "created_at",
        }
    ),
}
_GWIKI_COLUMNS: Mapping[str, frozenset[str]] = {
    "gwiki_documents": frozenset({"id"}),
    "gwiki_chunks": frozenset({"id", "document_id"}),
    "gwiki_sources": frozenset({"id"}),
}
_GCORE_CODE_INDEX_TABLES = frozenset(_GCORE_CODE_INDEX_COLUMNS)
_GWIKI_TABLES = frozenset(_GWIKI_COLUMNS)
_PG_SEARCH_MISSING_MESSAGE = (
    "pg_search extension is not present on this database. Rebuild the Docker PostgreSQL "
    "image with `gobby postgres install --mode docker`."
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

    def __init__(
        self,
        dsn: str,
        *,
        pool_config: PostgresPoolConfig = DEFAULT_POSTGRES_POOL_CONFIG,
    ) -> None:
        self._conninfo = _conninfo_with_utc_session_timezone(dsn)
        self._application_name = os.getenv("PGAPPNAME", "gobby")
        self._pool = ConnectionPool(
            conninfo=self._conninfo,
            open=False,
            min_size=pool_config.min_size,
            max_size=pool_config.max_size,
            timeout=pool_config.acquire_timeout_seconds,
            kwargs={
                "application_name": self._application_name,
                "prepare_threshold": None,
                "row_factory": dict_row,
            },
        )
        self._open_lock = threading.Lock()
        self._pool_opened = False
        self._pool_closed = False
        self._pool_open_timeout = pool_config.open_timeout_seconds
        _OPEN_DATABASES.add(self)

    def open(self, *, wait: bool = True, timeout: float | None = None) -> None:
        """Open the lazy connection pool before first use."""
        if getattr(self, "_pool_closed", False):
            raise RuntimeError(
                "PostgresHubDatabase connection pool is closed and cannot be reopened"
            )

        open_pool = getattr(self._pool, "open", None)
        if not callable(open_pool):
            return

        with self._open_lock:
            if getattr(self, "_pool_closed", False):
                raise RuntimeError(
                    "PostgresHubDatabase connection pool is closed and cannot be reopened"
                )
            if getattr(self, "_pool_opened", False):
                return
            open_timeout = timeout
            if open_timeout is None:
                open_timeout = self._pool_open_timeout
            open_pool(wait=wait, timeout=open_timeout)
            self._pool_opened = True

    def pool_stats(self) -> dict[str, Any]:
        """Return best-effort pool diagnostics for acquisition failures."""
        get_stats = getattr(self._pool, "get_stats", None)
        if not callable(get_stats):
            return {}
        try:
            return dict(get_stats())
        except Exception as exc:
            return {"pool_stats_error": f"{type(exc).__name__}: {exc}"}

    @contextmanager
    def _pool_connection(self) -> Iterator[psycopg.Connection[Any]]:
        with ExitStack() as stack:
            try:
                conn = stack.enter_context(self._pool.connection())
            except PoolTimeout:
                logger.warning(
                    "PostgreSQL hub pool acquisition timed out; checking pool before retry: "
                    "pool_stats=%s",
                    self.pool_stats(),
                )
                try:
                    self._pool.check()
                except Exception:
                    logger.warning(
                        "PostgreSQL hub pool check failed after acquisition timeout: pool_stats=%s",
                        self.pool_stats(),
                        exc_info=True,
                    )
                try:
                    conn = stack.enter_context(self._pool.connection())
                except PoolTimeout:
                    logger.warning(
                        "PostgreSQL hub pool acquisition retry failed: pool_stats=%s",
                        self.pool_stats(),
                        exc_info=True,
                    )
                    raise
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with enter_transaction(self, self._native_transaction) as txn:
            yield txn

    @contextmanager
    def transaction_immediate(self, lock: LockTarget) -> Iterator[Transaction]:
        with enter_transaction(self, self._native_transaction, immediate=True, lock=lock) as txn:
            yield txn

    @asynccontextmanager
    async def advisory_lock(self, lock: LockTarget) -> AsyncIterator[None]:
        """Hold typed PostgreSQL session locks without an idle transaction."""
        lock_keys = _advisory_lock_keys(lock)
        raw_conn, cancellation = await _await_task_completion(
            asyncio.create_task(asyncio.to_thread(self._open_advisory_lock_connection))
        )
        conn = cast(psycopg.Connection[Any], raw_conn)
        acquired = False

        try:
            if cancellation is not None:
                raise cancellation
            while not acquired:
                acquired_result, lock_cancellation = await _await_task_completion(
                    asyncio.create_task(
                        asyncio.to_thread(_try_session_advisory_locks, conn, lock_keys)
                    )
                )
                acquired = bool(acquired_result)
                if lock_cancellation is not None:
                    raise lock_cancellation
                if not acquired:
                    await asyncio.sleep(0.05)
            yield
        finally:
            await _close_advisory_lock_connection(conn, lock_keys if acquired else ())

    def _open_advisory_lock_connection(self) -> psycopg.Connection[Any]:
        """Open lock ownership outside the worker pool used by the protected body."""
        return psycopg.connect(
            self._conninfo,
            application_name=self._application_name,
            prepare_threshold=None,
            row_factory=dict_row,
        )

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
        with self._pool_connection() as conn, conn.transaction():
            txn = _PostgresTransaction(
                conn,
                is_immediate=is_immediate,
                initial_lock=initial_lock,
            )
            if initial_lock is not None:
                txn._acquire_lock_target(initial_lock)
            try:
                yield txn
            finally:
                txn.closed = True

        for callback in txn._after_commit_callbacks:
            try:
                callback()
            except Exception:
                logger.exception("PostgreSQL after-commit callback failed")

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        ambient = ambient_transaction(self)
        if ambient is not None:
            return ambient.execute(sql, params)
        with self.transaction() as txn:
            cursor = cast(_PostgresCursor, txn.execute(sql, params))
            return cursor.materialize()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Cursor:
        ambient = ambient_transaction(self)
        if ambient is not None:
            return ambient.executemany(sql, rows)
        with self.transaction() as txn:
            return txn.executemany(sql, rows)

    def after_commit(self, callback: Callable[[], None]) -> None:
        ambient = ambient_transaction(self)
        if ambient is None:
            callback()
            return
        ambient.after_commit(callback)

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
                    f"Pre-0.5 PostgreSQL hub databases below schema version {BASELINE_VERSION} "
                    f"require backup/export and recreation under Gobby baseline {BASELINE_VERSION}."
                )
            _require_baseline_extensions(conn)
            _verify_adopted_table_columns(conn, state)

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
        if getattr(self, "_pool_closed", False):
            return
        self._pool_closed = True
        # Daemon shutdown reserves three seconds after its 17-second async
        # cleanup deadline before the CLI force-kills the process at 20
        # seconds. Leave a one-second scheduling margin inside that tail.
        self._pool.close(timeout=_POOL_CLOSE_TIMEOUT_SECONDS)
        self._pool_opened = False


class _PostgresTransaction:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        is_immediate: bool = False,
        initial_lock: LockTarget | None = None,
    ) -> None:
        self._conn = conn
        self.is_immediate = is_immediate
        self.closed = False
        self._locks = [initial_lock] if initial_lock is not None else []
        self._after_commit_callbacks: list[Callable[[], Any]] = []

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
        if self.closed:
            callback()
            return
        self._after_commit_callbacks.append(callback)

    def acquire_additional_lock(self, lock: LockTarget) -> None:
        if not self.is_immediate:
            raise RuntimeError("additional locks require an immediate transaction")

        _acquire_lock(self._locks, lock)
        try:
            self._acquire_lock_target(lock)
        except Exception:
            self._locks.pop()
            raise

    def _acquire_lock_target(self, lock: LockTarget) -> None:
        if isinstance(lock, TaskSeqAllocation):
            self._acquire_advisory_lock(f"task_seq:{lock.project_id}")
            return

        for lock_key in _advisory_lock_keys(lock):
            self._acquire_advisory_lock(lock_key)

    def _acquire_advisory_lock(self, lock_key: str) -> None:
        self.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))


class _PostgresCursor:
    """Cursor adapter that can detach results from a checked-out connection.

    Non-ambient database execution materializes rows before returning its
    connection to the pool. The buffered cursor retains normal sequential
    ``fetchone`` and ``fetchall`` behavior after that checkout ends.
    """

    def __init__(self, cursor: Any | None, *, rowcount: int = -1) -> None:
        self._cursor = cursor
        self._rowcount = rowcount
        self._rows: list[Row] | None = None
        self._position = 0

    def materialize(self) -> _PostgresCursor:
        if self._cursor is None:
            return self
        rows = self.fetchall() if getattr(self._cursor, "description", None) is not None else []
        self._rowcount = self.rowcount
        self._cursor = None
        self._rows = rows
        self._position = 0
        return self

    def fetchone(self) -> Row | None:
        if self._rows is not None:
            if self._position >= len(self._rows):
                return None
            row = self._rows[self._position]
            self._position += 1
            return row
        if self._cursor is None:
            return None
        return _normalize_row(cast(Row | None, self._cursor.fetchone()))

    def fetchall(self) -> list[Row]:
        if self._rows is not None:
            rows = self._rows[self._position :]
            self._position = len(self._rows)
            return rows
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
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return to_aware_utc(value)
    if isinstance(value, date):
        return value
    if isinstance(value, dict | list):
        return json.dumps(to_json_safe(value), sort_keys=True, separators=(",", ":"))
    return value


def _conninfo_with_utc_session_timezone(conninfo: str) -> str:
    parsed = conninfo_to_dict(conninfo)
    raw_options = parsed.get("options")
    options = raw_options if isinstance(raw_options, str) else ""
    lower_options = options.lower()
    if "-ctimezone=" not in lower_options and "-c timezone=" not in lower_options:
        parsed["options"] = " ".join(part for part in (options, "-ctimezone=UTC") if part)
    return make_conninfo("", **parsed)


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
    if not has_bookkeeping and _GCORE_CODE_INDEX_TABLES.issubset(application_tables):
        return "gcore_code_index"
    if (
        not has_bookkeeping
        and _GWIKI_TABLES.issubset(application_tables)
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
        if state == "gcore_code_index" and _is_code_index_table_statement(statement):
            continue
        if _is_gwiki_table_statement(statement):
            continue
        if _is_adopted_index_statement(statement, state):
            statement = _add_index_if_not_exists(statement)
        yield statement


def _verify_adopted_table_columns(conn: Any, state: _BaselineState) -> None:
    contract: Mapping[str, frozenset[str]]
    if state == "gcore_code_index":
        contract = {**_GCORE_CODE_INDEX_COLUMNS, **_GWIKI_COLUMNS}
        required_tables = _GCORE_CODE_INDEX_TABLES
    elif state == "gwiki_standalone":
        contract = _GWIKI_COLUMNS
        required_tables = _GWIKI_TABLES
    else:
        return

    rows = conn.execute(
        """SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = ANY(%s)""",
        (list(contract),),
    ).fetchall()
    actual: dict[str, set[str]] = {table: set() for table in contract}
    for row in rows:
        table = str(_row_value(row, "table_name"))
        actual[table].add(str(_row_value(row, "column_name", 1)))

    missing = {
        table: sorted(expected - actual[table])
        for table, expected in contract.items()
        if (table in required_tables or actual[table]) and expected - actual[table]
    }
    if missing:
        details = "; ".join(
            f"{table}: {', '.join(columns)}" for table, columns in sorted(missing.items())
        )
        raise MigrationUnsupportedError(
            f"Cannot adopt external PostgreSQL schema; missing required columns: {details}"
        )


def _is_code_index_table_statement(statement: str) -> bool:
    return _is_create_table_statement_for(
        statement, lambda table: table in _GCORE_CODE_INDEX_TABLES
    )


def _is_gwiki_table_statement(statement: str) -> bool:
    return _is_create_table_statement_for(statement, _is_gwiki_table)


def _is_create_table_statement_for(
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

    return False


def _is_adopted_index_statement(statement: str, state: _BaselineState) -> bool:
    text = statement.strip()
    index_match = re.match(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"\"?[A-Za-z_][A-Za-z0-9_]*\"?"
        r"\s+ON\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?",
        text,
        re.IGNORECASE,
    )
    if index_match:
        table = index_match.group(1)
        return _is_gwiki_table(table) or (
            state == "gcore_code_index" and table in _GCORE_CODE_INDEX_TABLES
        )

    return False


def _add_index_if_not_exists(statement: str) -> str:
    return re.sub(
        r"^(\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+)",
        r"\1IF NOT EXISTS ",
        statement,
        count=1,
        flags=re.IGNORECASE,
    )


def _is_gwiki_table(table: str) -> bool:
    return table.startswith("gwiki_")


def _has_baseline_version(conn: Any, version: int) -> bool:
    row = conn.execute(
        "SELECT MAX(version) AS version FROM schema_migrations",
    ).fetchone()
    if row is None:
        return False
    max_version = _row_value(row, "version")
    return max_version is not None and int(max_version) >= version


def _require_extension(conn: Any, extension: str, message: str) -> None:
    row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = %s", (extension,)).fetchone()
    if row is None:
        raise MigrationUnsupportedError(message)


def _require_baseline_extensions(conn: Any) -> None:
    _require_extension(conn, "pg_search", _PG_SEARCH_MISSING_MESSAGE)


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

    # Table/column identifiers are allowlisted above; values remain parameterized.
    sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {where}"  # nosec
    return sql, (*update_params, *where_params)


def _validate_identifier(identifier: str) -> None:
    if not _SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier: {identifier!r}")


async def _await_task_completion(
    task: asyncio.Task[Any],
) -> tuple[Any, asyncio.CancelledError | None]:
    """Finish a thread-backed operation before propagating repeated cancellation."""
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(task), cancellation
        except asyncio.CancelledError as exc:
            if task.cancelled():
                raise
            cancellation = exc


def _try_session_advisory_locks(
    conn: psycopg.Connection[Any],
    lock_keys: tuple[str, ...],
) -> bool:
    acquired: list[str] = []
    try:
        for lock_key in lock_keys:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
                (lock_key,),
            ).fetchone()
            if row is not None and bool(row["acquired"]):
                acquired.append(lock_key)
                continue
            if acquired:
                _release_session_advisory_locks(conn, tuple(acquired))
            else:
                conn.commit()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        if acquired:
            _release_session_advisory_locks(conn, tuple(acquired))
        raise


def _release_session_advisory_locks(
    conn: psycopg.Connection[Any],
    lock_keys: tuple[str, ...],
) -> None:
    try:
        for lock_key in reversed(lock_keys):
            row = conn.execute(
                "SELECT pg_advisory_unlock(hashtext(%s)) AS released",
                (lock_key,),
            ).fetchone()
            if row is None or not bool(row["released"]):
                raise RuntimeError(f"PostgreSQL session advisory lock was not held: {lock_key}")
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise


async def _close_advisory_lock_connection(
    conn: psycopg.Connection[Any],
    lock_keys: tuple[str, ...],
) -> None:
    def close() -> None:
        try:
            if lock_keys:
                _release_session_advisory_locks(conn, lock_keys)
        finally:
            conn.close()

    _, cancellation = await _await_task_completion(asyncio.create_task(asyncio.to_thread(close)))
    if cancellation is not None:
        raise cancellation


def _advisory_lock_keys(lock: LockTarget) -> tuple[str, ...]:
    if isinstance(lock, BuildDryRunMutation):
        return (f"build_dry_run:{lock.project_id}",)
    if isinstance(lock, CronRunAdmission):
        return ("cron_run_admission",)
    if isinstance(lock, AgentCapAdmission):
        return (f"agent_cap_admission:{lock.project_id or '*'}",)
    if isinstance(lock, DispatchMutexRow):
        return (f"dispatch_mutex:{lock.task_id}",)
    if isinstance(lock, GitHubIssueTriageMutation):
        return (f"github_issue_triage:{lock.project_id}:{lock.repo}#{lock.issue_number}",)
    if isinstance(lock, IntegrationWorkspaceMutex):
        return (f"integration_workspace_mutex:{lock.integration_key}",)
    if isinstance(lock, SessionRegistration):
        return (
            "session_register:"
            f"{lock.external_id}|{lock.machine_id}|{lock.source}|"
            f"{lock.project_id or ''}|{lock.session_type}",
        )
    if isinstance(lock, SessionSeqMutation):
        return (f"session_seq:{lock.project_id}",)
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


def _acquire_lock(stack: list[LockTarget], lock: LockTarget) -> None:
    if stack:
        current = stack[-1]
        if lock.PRIORITY <= current.PRIORITY:
            raise LockAcquisitionOrderError(
                "nested lock priority must increase: "
                f"{current.PRIORITY} ({current}) -> {lock.PRIORITY} ({lock})"
            )
    stack.append(lock)
