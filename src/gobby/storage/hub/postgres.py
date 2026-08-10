"""PostgreSQL implementation of the hub database protocol."""

from __future__ import annotations

import atexit
import logging
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from functools import partial
from typing import Any, Literal, cast

import psycopg
from psycopg import sql as psycopg_sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from gobby.config.postgres_pool import DEFAULT_POSTGRES_POOL_CONFIG, PostgresPoolConfig
from gobby.deployment import deployment_token
from gobby.storage import schema_contract
from gobby.storage.concurrency import BOOTSTRAP_POOL_SIZE, PostgresCapacity
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

logger = logging.getLogger(__name__)

advisory_lock_keys = _postgres_pool.advisory_lock_keys
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


class PostgresHubDatabase:
    """Hub database adapter backed by psycopg and PostgreSQL."""

    dialect: Literal["postgres"] = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        pool_config: PostgresPoolConfig = DEFAULT_POSTGRES_POOL_CONFIG,
        runtime_role: str | None = None,
    ) -> None:
        if runtime_role is not None:
            _postgres_pool.validate_identifier(runtime_role)
        self._conninfo = _postgres_pool.conninfo_with_utc_session_timezone(dsn)
        self._runtime_role = runtime_role
        self._deployment_token = deployment_token()
        self._application_name = f"gobby-hub-{self._deployment_token}-{uuid.uuid4().hex[:8]}"
        runtime_configure = None
        runtime_check = None
        if runtime_role is not None:
            runtime_configure = partial(
                _postgres_pool.configure_runtime_role,
                runtime_role=runtime_role,
            )
            runtime_check = partial(
                _postgres_pool.assert_runtime_role,
                runtime_role=runtime_role,
            )
        self._pool = ConnectionPool(
            conninfo=self._conninfo,
            open=False,
            min_size=pool_config.min_size,
            max_size=pool_config.max_size,
            timeout=pool_config.acquire_timeout_seconds,
            max_lifetime=pool_config.max_lifetime_seconds,
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
            configure=runtime_configure,
            check=runtime_check,
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

    async def open_runtime_async_connection(self) -> psycopg.AsyncConnection[Any]:
        """Open a pool-exempt autocommit connection under the daemon runtime role."""
        runtime_role = self._runtime_role
        if runtime_role is None:
            raise RuntimeError("A runtime role is required for a pool-exempt connection")
        connection = await psycopg.AsyncConnection.connect(
            self._conninfo,
            autocommit=True,
            application_name=f"{self._application_name}-listener",
            connect_timeout=10,
            prepare_threshold=None,
            row_factory=dict_row,
            # An idle LISTEN socket can die silently on NAT/firewall timeouts;
            # keepalives surface the dead peer so the listener reconnects.
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=3,
        )
        try:
            statement = psycopg_sql.SQL("SET ROLE {}").format(psycopg_sql.Identifier(runtime_role))
            await connection.execute(statement)
            cursor = await connection.execute("SELECT current_user")
            row = await cursor.fetchone()
            observed = row.get("current_user") if isinstance(row, Mapping) else None
            if observed != runtime_role:
                raise _postgres_pool.RuntimeRoleMismatchError(
                    "PostgreSQL runtime role mismatch: "
                    f"expected {runtime_role!r}, observed {observed!r}"
                )
            return connection
        except BaseException:
            await connection.close()
            raise

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

    def server_capacity(self) -> PostgresCapacity:
        """Read the server connection settings used by daemon sizing."""
        self.open()
        with self._pool_connection() as conn:
            rows = conn.execute(
                "SELECT name, setting FROM pg_settings "
                "WHERE name IN ('max_connections', 'superuser_reserved_connections', "
                "'reserved_connections')"
            ).fetchall()
        settings = {str(_row_value(row, "name")): int(_row_value(row, "setting")) for row in rows}
        missing = {
            "max_connections",
            "superuser_reserved_connections",
        }.difference(settings)
        if missing:
            raise RuntimeError(
                "PostgreSQL did not report required connection settings: "
                + ", ".join(sorted(missing))
            )
        return PostgresCapacity(
            max_connections=settings["max_connections"],
            superuser_reserved_connections=settings["superuser_reserved_connections"],
            reserved_connections=settings.get("reserved_connections", 0),
        )

    def resize_pool(self, max_size: int) -> None:
        """Apply the resolved runtime pool size after ConfigStore loading."""
        if max_size < BOOTSTRAP_POOL_SIZE:
            raise ValueError(f"max_size must be at least {BOOTSTRAP_POOL_SIZE}")
        self.open()
        self._pool.resize(min_size=BOOTSTRAP_POOL_SIZE, max_size=max_size)

    def verify_runtime_identity(self) -> None:
        """Acquire and verify one connection from a served runtime-role pool."""
        if self._runtime_role is None:
            raise RuntimeError("PostgreSQL database is not configured with a runtime role")
        self.open()
        with _postgres_pool.pool_connection(self._pool, self.pool_stats) as conn:
            _postgres_pool.assert_runtime_role(conn, self._runtime_role)

    @contextmanager
    def _pool_connection(self) -> Iterator[psycopg.Connection[Any]]:
        with _postgres_pool.pool_connection(self._pool, self.pool_stats) as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        # PostgreSQL transactions can acquire additional advisory locks at any
        # point, so they are always safe to reuse for transaction_immediate().
        with _postgres_pool.transaction(
            self,
            self._native_transaction,
            immediate=True,
        ) as txn:
            yield txn

    @contextmanager
    def bounded_transaction(
        self,
        *,
        statement_timeout_ms: int = 5_000,
        lock_timeout_ms: int = 5_000,
        repeatable_read_read_only: bool = False,
    ) -> Iterator[Transaction]:
        """Open a transaction with server-enforced local operation bounds."""
        if statement_timeout_ms <= 0 or lock_timeout_ms <= 0:
            raise ValueError("Transaction bounds must be positive milliseconds")
        with self.transaction() as txn:
            if repeatable_read_read_only:
                txn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            settings = txn.execute(
                "SELECT current_setting('statement_timeout') AS statement_timeout, "
                "current_setting('lock_timeout') AS lock_timeout"
            ).fetchone()
            if settings is None:
                raise RuntimeError("Could not read transaction timeout settings")
            statement_timeout = str(settings["statement_timeout"])
            lock_timeout = str(settings["lock_timeout"])
            txn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{statement_timeout_ms}ms",),
            )
            txn.execute(
                "SELECT set_config('lock_timeout', %s, true)",
                (f"{lock_timeout_ms}ms",),
            )
            try:
                yield txn
            finally:
                txn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (statement_timeout,),
                )
                txn.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (lock_timeout,),
                )

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
            cursor = cast(_postgres_pool.PostgresCursor, txn.execute(sql, params))
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
            return _postgres_pool.PostgresCursor(None, rowcount=0)
        sql, params = built
        return self.execute(sql, params)

    def apply_migrations(self) -> None:
        schema_contract.apply_schema(self._conninfo)

    def apply_destructive_migrations(self) -> None:
        """Apply or resume one verified destructive migration batch."""
        schema_contract.apply_schema(self._conninfo, destructive=True)

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
    _postgres_pool.validate_identifier(table)

    update_params: list[Any] = []
    set_clauses: list[str] = []
    for column, value in values.items():
        _postgres_pool.validate_identifier(column)
        set_clauses.append(f"{column} = %s")
        update_params.append(value)

    # Table/column identifiers are allowlisted above; values remain parameterized.
    sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {where}"  # nosec
    return sql, (*update_params, *where_params)
