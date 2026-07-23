"""PostgreSQL implementation of the hub database protocol."""

from __future__ import annotations

import atexit
import importlib.resources
import logging
import re
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Literal, cast

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gobby.config.postgres_pool import DEFAULT_POSTGRES_POOL_CONFIG, PostgresPoolConfig
from gobby.deployment import deployment_token
from gobby.storage.hub import postgres_pool as _postgres_pool
from gobby.storage.hub._ambient import ambient_transaction
from gobby.storage.hub.protocol import (
    AgentCapAdmission as AgentCapAdmission,
)
from gobby.storage.hub.protocol import (
    Cursor,
    LockTarget,
    Row,
    Transaction,
)
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MigrationRunner,
    MigrationUnsupportedError,
    _split_statements_respecting_dollar_quotes,
)

logger = logging.getLogger(__name__)

_advisory_lock_keys = _postgres_pool._advisory_lock_keys
_OPEN_DATABASES: set[PostgresHubDatabase] = set()
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
    "image with `gobby postgres install`."
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
        self._conninfo = _postgres_pool._conninfo_with_utc_session_timezone(dsn)
        self._deployment_token = deployment_token()
        self._application_name = f"gobby-hub-{self._deployment_token}-{uuid.uuid4().hex[:8]}"
        self._pool = ConnectionPool(
            conninfo=self._conninfo,
            open=False,
            min_size=pool_config.min_size,
            max_size=pool_config.max_size,
            timeout=pool_config.acquire_timeout_seconds,
            kwargs={
                "application_name": self._application_name,
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                "prepare_threshold": None,
                "row_factory": dict_row,
            },
        )
        self._open_lock = threading.Lock()
        self._pool_opened = False
        self._pool_closed = False
        self._pool_open_timeout = pool_config.open_timeout_seconds
        _OPEN_DATABASES.add(self)

    @property
    def conninfo(self) -> str:
        """Return the normalized connection string without exposing the sync pool."""
        return self._conninfo

    @property
    def application_name(self) -> str:
        """Return this lifecycle's unique hub-backend marker."""
        return self._application_name

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
        with _postgres_pool.pool_connection(self._pool, self.pool_stats) as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        with _postgres_pool.transaction(self, self._native_transaction) as txn:
            yield txn

    @contextmanager
    def bounded_transaction(
        self,
        *,
        statement_timeout_ms: int = 5_000,
        lock_timeout_ms: int = 5_000,
    ) -> Iterator[Transaction]:
        """Open a transaction with server-enforced local operation bounds."""
        if statement_timeout_ms <= 0 or lock_timeout_ms <= 0:
            raise ValueError("Transaction bounds must be positive milliseconds")
        with self.transaction() as txn:
            txn.execute(f"SET LOCAL statement_timeout = '{statement_timeout_ms}ms'")
            txn.execute(f"SET LOCAL lock_timeout = '{lock_timeout_ms}ms'")
            yield txn

    @contextmanager
    def transaction_immediate(self, lock: LockTarget) -> Iterator[Transaction]:
        with _postgres_pool.transaction(
            self,
            self._native_transaction,
            immediate=True,
            lock=lock,
        ) as txn:
            yield txn

    @asynccontextmanager
    async def advisory_lock(self, lock: LockTarget) -> AsyncIterator[None]:
        async with _postgres_pool.advisory_lock(
            self._conninfo,
            self._application_name,
            lock,
        ):
            yield

    def _open_advisory_lock_connection(self) -> psycopg.Connection[Any]:
        return _postgres_pool.open_advisory_lock_connection(
            self._conninfo,
            self._application_name,
        )

    @contextmanager
    def _native_transaction(
        self,
        *,
        immediate: bool,
        lock: LockTarget | None,
    ) -> Iterator[Transaction]:
        with _postgres_pool.native_transaction(
            self._transaction_context,
            immediate=immediate,
            lock=lock,
        ) as txn:
            yield txn

    @contextmanager
    def _transaction_context(
        self,
        *,
        is_immediate: bool,
        initial_lock: LockTarget | None = None,
    ) -> Iterator[Transaction]:
        with _postgres_pool.transaction_context(
            self.open,
            self._pool_connection,
            is_immediate=is_immediate,
            initial_lock=initial_lock,
        ) as txn:
            yield txn

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        ambient = ambient_transaction(self)
        if ambient is not None:
            return ambient.execute(sql, params)
        with self.transaction() as txn:
            cursor = cast(_postgres_pool._PostgresCursor, txn.execute(sql, params))
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
            return _postgres_pool._PostgresCursor(None, rowcount=0)
        sql, params = built
        return self.execute(sql, params)

    def apply_migrations(self) -> None:
        runner = MigrationRunner(self, autocommit_connection=self._open_advisory_lock_connection)
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
                tables = _schema_tables(conn)
                if "schema_migrations" in tables:  # Flattened baseline has no upgrade path.
                    row = conn.execute(
                        "SELECT MAX(version) AS version FROM schema_migrations"
                    ).fetchone()
                    version = None if row is None else _row_value(row, "version")
                    observed_tables = sorted(tables - _BASELINE_BOOKKEEPING_TABLES)
                    raise MigrationUnsupportedError(
                        f"Unsupported pre-{BASELINE_VERSION} PostgreSQL baseline lineage: "
                        f"observed max schema version {version!r} with tables {observed_tables!r}. "
                        "Post-baseline repair migrations do not run for this lineage; "
                        "back up/export the database and recreate it."
                    )
                raise MigrationUnsupportedError("Unrecognized PostgreSQL schema.")
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
        # Daemon shutdown reserves three seconds after its 17-second async
        # cleanup deadline before the CLI force-kills the process at 20
        # seconds. Leave a one-second scheduling margin inside that tail.
        self._pool.close(timeout=_POOL_CLOSE_TIMEOUT_SECONDS)
        self._pool_opened = False
        self._pool_closed = True
        _OPEN_DATABASES.discard(self)


def _classify_baseline_state(conn: Any) -> _BaselineState:
    tables = _schema_tables(conn)
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


def _schema_tables(conn: Any) -> set[str]:
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname=CURRENT_SCHEMA"
    ).fetchall()
    return {str(_row_value(row, "tablename")) for row in rows}


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
    _postgres_pool._validate_identifier(identifier)
