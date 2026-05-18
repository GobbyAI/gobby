"""SQLite database manager for local storage."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

# Register custom datetime adapters/converters (required since Python 3.12)
# See: https://docs.python.org/3/library/sqlite3.html#default-adapters-and-converters-deprecated


def _adapt_datetime(val: datetime) -> str:
    """Adapt datetime to ISO format string for SQLite storage."""
    # If naive datetime, assume UTC and add timezone info for RFC3339 compliance
    if val.tzinfo is None:
        val = val.replace(tzinfo=UTC)
    return val.isoformat()


def _adapt_date(val: date) -> str:
    """Adapt date to ISO format string for SQLite storage."""
    return val.isoformat()


def _convert_datetime(val: bytes) -> datetime:
    """Convert SQLite datetime string back to datetime object."""
    dt = datetime.fromisoformat(val.decode())
    # Ensure timezone-aware (treat naive as UTC) for consistency
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _convert_date(val: bytes) -> date:
    """Convert SQLite date string back to date object."""
    return date.fromisoformat(val.decode())


# Register adapters (Python -> SQLite)
sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_adapter(date, _adapt_date)

# Register converters (SQLite -> Python) - used with detect_types
sqlite3.register_converter("datetime", _convert_datetime)
sqlite3.register_converter("date", _convert_date)

logger = logging.getLogger(__name__)

_SQLITE_BUSY_TIMEOUT_MS = 10_000
_DB_CONNECTION_WARNING_THRESHOLD = 32


@runtime_checkable
class DatabaseProtocol(Protocol):
    """Protocol defining the database interface for storage managers."""

    @property
    def db_path(self) -> Any:
        """Return database path."""
        ...

    @property
    def connection(self) -> sqlite3.Connection:
        """Get database connection (for reads)."""
        ...

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute SQL statement."""
        ...

    def executemany(self, sql: str, params_list: list[tuple[Any, ...]]) -> sqlite3.Cursor:
        """Execute SQL statement with multiple parameter sets."""
        ...

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """Execute query and fetch one row."""
        ...

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Execute query and fetch all rows."""
        ...

    def safe_update(
        self,
        table: str,
        values: dict[str, Any],
        where: str,
        where_params: tuple[Any, ...],
    ) -> sqlite3.Cursor:
        """Safely execute an UPDATE statement with dynamic columns."""
        ...

    def transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        """Context manager for database transactions."""
        ...

    def transaction_immediate(self) -> AbstractContextManager[sqlite3.Connection]:
        """Context manager for IMMEDIATE transactions (write-intent).

        Acquires write lock at BEGIN, preventing concurrent read-modify-write races.
        Use for atomic read-then-update patterns where deferred locking is insufficient.
        """
        ...

    def close(self) -> None:
        """Close database connection."""
        ...

    @property
    def connection_count(self) -> int:
        """Return the number of currently tracked open connections."""
        ...


# Production database path (constant for comparison in safety checks)
_PRODUCTION_DB_PATH = (Path.home() / ".gobby" / "gobby-hub.db").resolve()


def _default_db_path() -> Path:
    """Compute default DB path, respecting GOBBY_HOME env var at runtime."""
    gobby_home = os.environ.get("GOBBY_HOME")
    if gobby_home:
        return Path(gobby_home) / "gobby-hub.db"
    return Path.home() / ".gobby" / "gobby-hub.db"


# SQL identifier validation pattern (alphanumeric + underscore only)
# Used by safe_update to prevent SQL injection via column/table names
_SQL_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class _ThreadConnectionLease:
    """Weakref-able owner for a thread-local SQLite connection.

    ``threading.local`` cannot notify us when a worker thread exits, so each
    per-thread connection gets a tiny lease object stored in that thread-local
    state. ``LocalDatabase`` registers a weakref finalizer against the lease;
    when the thread-local state is collected, the finalizer closes and untracks
    the SQLite connection even if the owning thread has already disappeared.
    """

    __slots__ = ("__weakref__",)


class LocalDatabase:
    """
    SQLite database manager with connection pooling.

    Thread-safe connection management using thread-local storage.
    """

    def __init__(self, db_path: Path | str | None = None):
        """
        Initialize database manager.

        Args:
            db_path: Path to SQLite database file. Defaults to ~/.gobby/gobby-hub.db
        """
        # SAFETY SWITCH: During tests, prevent any access to the production database.
        # Catches both db_path=None (default) and explicit paths that resolve to production.
        if os.environ.get("GOBBY_TEST_PROTECT") == "1":
            safe_path = os.environ.get("GOBBY_DATABASE_PATH")
            if db_path is None:
                if safe_path:
                    db_path = safe_path
                else:
                    raise RuntimeError(
                        "GOBBY_TEST_PROTECT is set but no GOBBY_DATABASE_PATH configured. "
                        "Refusing to fall through to default (possibly production) database."
                    )
            else:
                resolved = Path(db_path).expanduser().resolve()
                if resolved == _PRODUCTION_DB_PATH:
                    if safe_path:
                        db_path = safe_path
                    else:
                        raise RuntimeError(
                            f"Test attempted to open production database: {resolved}"
                        )

        self.db_path = Path(db_path) if db_path else _default_db_path()
        self._local = threading.local()
        # Track all connections for proper cleanup across threads
        self._all_connections: set[sqlite3.Connection] = set()
        self._connections_lock = threading.Lock()
        self._closed = False
        self._last_connection_warning_count = 0
        self._ensure_directory()

        self._finalizer = weakref.finalize(
            self,
            self._close_tracked_connections,
            self._all_connections,
            self._connections_lock,
            self._local,
        )

    def _ensure_directory(self) -> None:
        """Create database directory if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_connection(self) -> sqlite3.Connection:
        """Get thread-local database connection."""
        if self._closed:
            raise RuntimeError(f"LocalDatabase is closed: {self.db_path}")

        if not hasattr(self._local, "connection") or self._local.connection is None:
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                isolation_level=None,  # Autocommit mode
                timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000,
            )
            conn.row_factory = sqlite3.Row
            # Enable foreign keys
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
            # Last-resort safety: if test somehow connects to production DB, block writes
            if os.environ.get("GOBBY_TEST_PROTECT") == "1":
                if self.db_path.resolve() == _PRODUCTION_DB_PATH:
                    conn.execute("PRAGMA query_only = ON")
            # Use default DELETE journal mode (more reliable than WAL for dual-write)
            self._local.connection = conn
            lease = _ThreadConnectionLease()
            self._local.connection_lease = lease
            weakref.finalize(
                lease,
                self._close_connection,
                self._all_connections,
                self._connections_lock,
                conn,
            )
            # Track for cleanup in close()
            with self._connections_lock:
                self._all_connections.add(conn)
                connection_count = len(self._all_connections)
                if connection_count <= _DB_CONNECTION_WARNING_THRESHOLD:
                    self._last_connection_warning_count = 0
                elif connection_count > self._last_connection_warning_count:
                    self._last_connection_warning_count = connection_count
                    logger.warning(
                        "LocalDatabase has %d open SQLite connection(s) for %s",
                        connection_count,
                        self.db_path,
                    )
        return cast(sqlite3.Connection, self._local.connection)

    @property
    def connection_count(self) -> int:
        """Return the number of currently tracked open connections."""
        with self._connections_lock:
            return len(self._all_connections)

    @property
    def connection(self) -> sqlite3.Connection:
        """Get current thread's database connection."""
        return self._get_connection()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute SQL statement."""
        return self.connection.execute(sql, params)

    def executemany(self, sql: str, params_list: list[tuple[Any, ...]]) -> sqlite3.Cursor:
        """Execute SQL statement with multiple parameter sets."""
        return self.connection.executemany(sql, params_list)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """Execute query and fetch one row."""
        cursor = self.execute(sql, params)
        try:
            return cast(sqlite3.Row | None, cursor.fetchone())
        finally:
            cursor.close()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Execute query and fetch all rows."""
        cursor = self.execute(sql, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def schema_version(self) -> int:
        """Return the current SQLite schema version recorded in schema_version."""
        from gobby.storage.migrations import get_current_version

        return get_current_version(self)

    def migrations_needed(self) -> bool:
        """Return whether this database needs schema migration work.

        The check only compares stored schema_version with the latest version
        known to this build. It does not run startup repair routines.
        """
        from gobby.storage.migrations import migrations_needed

        return migrations_needed(self)

    def safe_update(
        self,
        table: str,
        values: dict[str, Any],
        where: str,
        where_params: tuple[Any, ...],
    ) -> sqlite3.Cursor:
        """
        Safely execute an UPDATE statement with dynamic columns.

        This method validates table and column names against a strict allowlist
        pattern to prevent SQL injection, even though callers typically use
        hardcoded strings. This is defense-in-depth.

        Args:
            table: Table name (validated against identifier pattern).
            values: Dictionary of column_name -> new_value.
            where: WHERE clause (e.g., "id = ?"). This is NOT validated -
                   callers must use parameterized queries for values.
            where_params: Parameters for the WHERE clause placeholders.

        Returns:
            sqlite3.Cursor from the executed statement.

        Raises:
            ValueError: If table or column names fail validation.

        Example:
            db.safe_update(
                "sessions",
                {"status": "closed", "updated_at": now},
                "id = ?",
                (session_id,)
            )
        """
        if not values:
            # No-op: return closed cursor without executing
            cursor = self.connection.cursor()
            cursor.close()
            return cursor

        # Validate table name
        if not _SQL_IDENTIFIER_PATTERN.match(table):
            raise ValueError(f"Invalid table name: {table!r}")

        # Validate column names and build SET clause
        set_clauses: list[str] = []
        update_params: list[Any] = []

        for col, val in values.items():
            if not _SQL_IDENTIFIER_PATTERN.match(col):
                raise ValueError(f"Invalid column name: {col!r}")
            set_clauses.append(f"{col} = ?")
            update_params.append(val)

        # Construct and execute query
        sql = f"UPDATE {table} SET {', '.join(set_clauses)} WHERE {where}"  # nosec B608
        full_params = tuple(update_params) + where_params

        return self.execute(sql, full_params)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """
        Context manager for database transactions.

        Usage:
            with db.transaction() as conn:
                conn.execute("INSERT ...")
                conn.execute("UPDATE ...")

        Tolerates inner code that implicitly commits the transaction (e.g.
        ``Connection.executescript`` always issues an implicit COMMIT before
        running). In that case the outer COMMIT/ROLLBACK is skipped because
        there is nothing left to finalize — guarded by ``conn.in_transaction``.
        """
        conn = self.connection
        if conn.in_transaction:
            savepoint = self._next_savepoint_name()
            self._push_after_commit_scope()
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield conn
                if conn.in_transaction:
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._pop_after_commit_scope(committed=True)
            except Exception:
                if conn.in_transaction:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._pop_after_commit_scope(committed=False)
                raise
            return
        self._push_after_commit_scope()
        conn.execute("BEGIN")
        try:
            yield conn
            if conn.in_transaction:
                conn.execute("COMMIT")
            self._run_after_commit_callbacks()
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            self._pop_after_commit_scope(committed=False)
            raise

    @contextmanager
    def transaction_immediate(self) -> Iterator[sqlite3.Connection]:
        """Context manager for IMMEDIATE transactions (write-intent).

        Acquires write lock at BEGIN, preventing concurrent read-modify-write races.
        Use for atomic read-then-update patterns where deferred locking is insufficient.

        Tolerates inner code that implicitly commits the transaction (e.g.
        ``Connection.executescript``). See ``transaction`` for details.
        """
        conn = self.connection
        if conn.in_transaction:
            savepoint = self._next_savepoint_name()
            self._push_after_commit_scope()
            conn.execute(f"SAVEPOINT {savepoint}")
            try:
                yield conn
                if conn.in_transaction:
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._pop_after_commit_scope(committed=True)
            except Exception:
                if conn.in_transaction:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                self._pop_after_commit_scope(committed=False)
                raise
            return
        self._push_after_commit_scope()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if conn.in_transaction:
                conn.execute("COMMIT")
            self._run_after_commit_callbacks()
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            self._pop_after_commit_scope(committed=False)
            raise

    def _next_savepoint_name(self) -> str:
        """Generate a per-thread savepoint name for nested transactions."""
        counter = getattr(self._local, "savepoint_counter", 0) + 1
        self._local.savepoint_counter = counter
        return f"gobby_sp_{counter}"

    def after_commit(self, callback: Callable[[], Any]) -> None:
        """Run a callback after the current transaction commits.

        When called outside a managed transaction, the callback runs immediately.
        Nested transactions defer callbacks until the outermost commit succeeds.
        """
        stack = getattr(self._local, "after_commit_stack", None)
        if not stack:
            callback()
            return
        stack[-1].append(callback)

    def _push_after_commit_scope(self) -> None:
        """Track callbacks registered within the current transaction scope."""
        stack = cast(
            list[list[Callable[[], Any]]] | None,
            getattr(self._local, "after_commit_stack", None),
        )
        if stack is None:
            stack = []
            self._local.after_commit_stack = stack
        stack.append([])

    def _pop_after_commit_scope(self, *, committed: bool) -> list[Callable[[], Any]]:
        """Resolve callbacks for a transaction scope.

        On nested commit, callbacks bubble up to the parent scope. On rollback,
        the scope's callbacks are discarded.
        """
        stack = cast(
            list[list[Callable[[], Any]]] | None,
            getattr(self._local, "after_commit_stack", None),
        )
        if not stack:
            return []

        callbacks = stack.pop()
        if committed and stack:
            stack[-1].extend(callbacks)
            callbacks = []
        if not stack:
            self._local.after_commit_stack = []
        return callbacks

    def _run_after_commit_callbacks(self) -> None:
        """Run callbacks captured in the just-committed outer transaction."""
        for callback in self._pop_after_commit_scope(committed=True):
            callback()

    @staticmethod
    def _close_connection(
        connections: set[sqlite3.Connection],
        connections_lock: threading.Lock,
        conn: sqlite3.Connection,
    ) -> None:
        """Close one tracked connection without retaining the LocalDatabase instance."""
        with connections_lock:
            connections.discard(conn)

        try:
            conn.close()
        except Exception as e:
            logger.debug("Connection close failed: %s", e)

    @staticmethod
    def _close_tracked_connections(
        connections: set[sqlite3.Connection],
        connections_lock: threading.Lock,
        local_state: threading.local,
    ) -> None:
        """Close tracked connections without retaining the LocalDatabase instance."""
        with connections_lock:
            tracked_connections = list(connections)
            connections.clear()

        for conn in tracked_connections:
            try:
                conn.close()
            except Exception as e:
                logger.debug("Connection close failed: %s", e)

        if hasattr(local_state, "connection"):
            local_state.connection = None
        if hasattr(local_state, "connection_lease"):
            local_state.connection_lease = None

    def close(self) -> None:
        """Close all database connections and reject future use.

        Can be called explicitly or via context manager. For automatic cleanup
        at interpreter shutdown, weakref.finalize handles tracked connections
        without keeping this LocalDatabase instance alive.
        """
        if self._closed:
            return

        self._closed = True
        self._finalizer()

    def __enter__(self) -> LocalDatabase:
        """Enter context manager."""
        return self

    def __exit__(self, exc_type: type | None, exc_val: Exception | None, exc_tb: object) -> None:
        """Exit context manager, closing connections."""
        self.close()
