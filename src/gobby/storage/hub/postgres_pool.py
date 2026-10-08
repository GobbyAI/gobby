"""PostgreSQL connection, transaction, and cursor machinery."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import select
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import (
    AbstractContextManager,
    ExitStack,
    asynccontextmanager,
    contextmanager,
    nullcontext,
)
from datetime import date, datetime
from typing import Any, Protocol, cast

import psycopg
from psycopg import sql as psycopg_sql
from psycopg.abc import Buffer
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.postgres import types as pg_types
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.string import TextLoader
from psycopg_pool import ConnectionPool, PoolTimeout

from gobby.storage.hub._ambient import TransactionOpener, enter_transaction
from gobby.storage.hub.async_ops import IndeterminateCommitError
from gobby.storage.hub.operation_deadline import current_database_operation_deadline
from gobby.storage.hub.protocol import (
    AgentCapAdmission,
    AgentTaskClaimMutation,
    BuildDryRunMutation,
    ChatAttachmentMutation,
    CronRunAdmission,
    Cursor,
    DispatchMutexRow,
    IntegrationWorkspaceMutex,
    IsolationRegistryReconciliation,
    LockAcquisitionOrderError,
    LockTarget,
    MainCheckoutLanding,
    PlanReviewEvidenceMutation,
    ReviewLearningPatternMutation,
    Row,
    Savepoint,
    SessionLineageMutation,
    SessionRecoveryByProject,
    SessionRegistration,
    SessionSeqMutation,
    SessionVariableMutation,
    SessionVariableTransfer,
    StageReviewApprovalMutation,
    StageReviewRejectionMutation,
    SystemSessionBootstrap,
    TaskLifecycleMutation,
    TaskSeqAllocation,
    TaskSubtreeCascade,
    Transaction,
    WebChatSessionBootstrap,
)
from gobby.storage.hub.read_scope import record_closed_writes, written_tables
from gobby.storage.hub.transaction_deadline import TransactionDeadline
from gobby.telemetry.instruments import observe_histogram
from gobby.telemetry.query_timing import record_pool_acquire, record_query
from gobby.utils.datetime import to_aware_utc, to_json_safe

logger = logging.getLogger(__name__)

POOL_TIMEOUT_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0)
POOL_TIMEOUT_RETRY_JITTER_RATIO = 0.25

# psycopg parses json/jsonb into plain JSON values, which need no datetime walk.
_JSON_TYPE_OIDS = frozenset(pg_types[name].oid for name in ("json", "jsonb"))
_PLAIN_SCALAR_TYPES = frozenset({str, int, float, bool, type(None)})

_SQL_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_POOL_UNAVAILABLE_MESSAGE_MARKERS = (
    "couldn't get a connection",
    "terminating connection",
    "connection is closed",
    "connection not open",
)


class RuntimeRoleMismatchError(RuntimeError):
    """Raised when a served pool connection is not using its fixed runtime role."""


def is_pool_unavailable(exc: BaseException) -> bool:
    """Classify an exception as a transient hub pool outage.

    Matches PoolTimeout and psycopg operational failures whose message
    indicates dead or unavailable pool connections, including causes wrapped
    by ``raise ... from ...``.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, PoolTimeout):
            return True
        if isinstance(current, psycopg.OperationalError):
            message = str(current).lower()
            if any(marker in message for marker in _POOL_UNAVAILABLE_MESSAGE_MARKERS):
                return True
        current = current.__cause__ or current.__context__
    return False


class _TransactionContext(Protocol):
    def __call__(
        self,
        *,
        is_immediate: bool,
        initial_lock: LockTarget | None = None,
    ) -> AbstractContextManager[Transaction]: ...


@contextmanager
def pool_connection(
    pool: ConnectionPool[Any],
    pool_stats: Callable[[], dict[str, Any]],
    *,
    acquire_timeout_seconds: float | None = None,
) -> Iterator[psycopg.Connection[Any]]:
    """Acquire a pooled connection within its caller's ownership policy.

    Ordinary callers retry with backoff because dead connections can linger
    after a hub restart. Deadline-owned callers make one bounded attempt so a
    cancelled workflow cannot leave the retry chain occupying its worker.
    """
    started = time.monotonic()
    with ExitStack() as stack:
        try:
            deadline = current_database_operation_deadline()
            if deadline is None:
                try:
                    conn = stack.enter_context(pool.connection())
                except PoolTimeout:
                    conn = _acquire_with_backoff(stack, pool, pool_stats)
            else:
                timeout = deadline.remaining_seconds(
                    maximum_seconds=acquire_timeout_seconds,
                )
                try:
                    conn = stack.enter_context(pool.connection(timeout=timeout))
                except PoolTimeout:
                    logger.debug(
                        "PostgreSQL hub pool acquisition reached its owning operation "
                        "bound; skipping retries: pool_stats=%s",
                        pool_stats(),
                    )
                    raise
        except BaseException:
            _observe_acquire_wait(time.monotonic() - started)
            raise
        _observe_acquire_wait(time.monotonic() - started)
        yield conn


def _observe_acquire_wait(wait_seconds: float) -> None:
    observe_histogram("database_pool_acquire_wait_seconds", wait_seconds)
    record_pool_acquire(wait_seconds)


def _acquire_with_backoff(
    stack: ExitStack,
    pool: ConnectionPool[Any],
    pool_stats: Callable[[], dict[str, Any]],
) -> psycopg.Connection[Any]:
    """Retry pool acquisition after an initial PoolTimeout with backoff."""
    for attempt, backoff in enumerate(POOL_TIMEOUT_RETRY_BACKOFF_SECONDS, start=1):
        logger.debug(
            "PostgreSQL hub pool acquisition timed out (attempt %d/%d); "
            "retrying in %.2fs: pool_stats=%s",
            attempt,
            len(POOL_TIMEOUT_RETRY_BACKOFF_SECONDS),
            backoff,
            pool_stats(),
        )
        pool.check()
        time.sleep(
            backoff * (1 + random.uniform(0, POOL_TIMEOUT_RETRY_JITTER_RATIO))  # nosec B311  # jitter, not crypto
        )
        try:
            return stack.enter_context(pool.connection())
        except PoolTimeout:
            continue
    logger.error(
        "PostgreSQL hub pool acquisition failed after %d retries: pool_stats=%s",
        len(POOL_TIMEOUT_RETRY_BACKOFF_SECONDS),
        pool_stats(),
        exc_info=True,
    )
    raise


@contextmanager
def transaction(
    adapter: object,
    opener: TransactionOpener,
    *,
    immediate: bool = False,
    lock: LockTarget | None = None,
) -> Iterator[Transaction]:
    """Enter a regular or immediate ambient transaction."""
    with enter_transaction(adapter, opener, immediate=immediate, lock=lock) as txn:
        yield txn


@asynccontextmanager
async def advisory_lock(
    conninfo: str,
    application_name: str,
    lock: LockTarget,
) -> AsyncIterator[None]:
    """Hold typed PostgreSQL session locks without an idle transaction."""
    lock_keys = advisory_lock_keys(lock)
    raw_conn, cancellation = await _await_task_completion(
        asyncio.create_task(
            asyncio.to_thread(open_advisory_lock_connection, conninfo, application_name)
        )
    )
    conn = cast(psycopg.Connection[Any], raw_conn)
    acquired = False

    try:
        if cancellation is not None:
            raise cancellation
        while not acquired:
            acquired_result, lock_cancellation = await _await_task_completion(
                asyncio.create_task(asyncio.to_thread(_try_session_advisory_locks, conn, lock_keys))
            )
            acquired = bool(acquired_result)
            if lock_cancellation is not None:
                raise lock_cancellation
            if not acquired:
                await asyncio.sleep(0.05)
        yield
    finally:
        await _close_advisory_lock_connection(conn, lock_keys if acquired else ())


def open_advisory_lock_connection(
    conninfo: str,
    application_name: str,
) -> psycopg.Connection[Any]:
    """Open lock ownership outside the worker pool used by the protected body."""
    return psycopg.connect(
        conninfo,
        application_name=application_name,
        prepare_threshold=None,
        autocommit=True,
        row_factory=dict_row,
    )


@contextmanager
def native_transaction(
    transaction_context: _TransactionContext,
    *,
    immediate: bool,
    lock: LockTarget | None,
) -> Iterator[Transaction]:
    """Open one native PostgreSQL transaction."""
    with transaction_context(is_immediate=immediate, initial_lock=lock) as txn:
        yield txn


@contextmanager
def transaction_context(
    open_pool: Callable[[], None],
    connection: Callable[[], AbstractContextManager[psycopg.Connection[Any]]],
    *,
    is_immediate: bool,
    initial_lock: LockTarget | None = None,
) -> Iterator[Transaction]:
    """Own a pooled connection and run callbacks after its transaction commits."""
    open_pool()
    with connection() as conn:
        txn = _PostgresTransaction(
            conn,
            is_immediate=is_immediate,
            initial_lock=initial_lock,
        )
        commit_submitted = False
        try:
            with conn.transaction():
                if initial_lock is not None:
                    txn._acquire_lock_target(initial_lock)
                yield txn
                # PostgreSQL answers COMMIT of an aborted transaction with
                # ROLLBACK and psycopg reports success, so a swallowed statement
                # error would silently discard every write and still run the
                # after-commit callbacks (#23296). Code that recovers from a
                # statement error must do so inside a savepoint.
                if conn.info.transaction_status == TransactionStatus.INERROR:
                    raise psycopg.errors.InFailedSqlTransaction(
                        "transaction aborted by a swallowed statement error"
                    )
                txn._deadline.prepare()
                commit_submitted = True
        except BaseException as exc:
            server_rejected = isinstance(exc, psycopg.Error) and (
                exc.diag.severity_nonlocalized == "ERROR"
                or (exc.sqlstate is not None and exc.sqlstate[:2] in {"23", "40"})
            )
            # Preserve definite server rejection, including deferred constraints.
            # Cancellation and an explicitly unknown outcome retain the existing
            # conservative contract even when they carry a server diagnostic.
            uncertain_error = isinstance(
                exc,
                (
                    psycopg.errors.QueryCanceled,
                    psycopg.errors.LockNotAvailable,
                    psycopg.errors.StatementCompletionUnknown,
                ),
            )
            if commit_submitted and (not server_rejected or uncertain_error):
                raise IndeterminateCommitError(
                    "PostgreSQL COMMIT outcome was not observed"
                ) from exc
            raise
        finally:
            txn.closed = True
            # Stamped after COMMIT or ROLLBACK so a concurrent hub_read_scope
            # never keeps a row loaded before this write became visible.
            if txn._written != set():
                record_closed_writes(None if txn._written is None else frozenset(txn._written))

    for callback in txn._after_commit_callbacks:
        try:
            callback()
        except Exception:
            logger.exception("PostgreSQL after-commit callback failed")


def is_autocommit_read(sql: str) -> bool:
    """Whether ``sql`` is one read-only statement that needs no BEGIN/COMMIT.

    Only SELECT and a write-free WITH qualify: a plain SET would persist on the
    pooled session, and SAVEPOINT or RELEASE need a transaction block. A ``;``
    anywhere but the end, even inside a literal, keeps the statement transactional.
    """
    statement = sql.strip().removesuffix(";")
    return (
        ";" not in statement
        and statement[:6].upper().startswith(("SELECT", "WITH"))
        and written_tables(statement) == frozenset()
    )


def autocommit_read(
    open_pool: Callable[[], None],
    connection: Callable[[], AbstractContextManager[psycopg.Connection[Any]]],
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] = (),
) -> PostgresCursor:
    """Run one read-only statement without the BEGIN/COMMIT round trips (#23359).

    PostgreSQL runs a lone statement atomically either way, so autocommit only
    drops the transaction bookkeeping around it. Rows are buffered before the
    connection returns to the pool in the mode it was handed over in.
    """
    open_pool()
    with connection() as conn:
        conn.autocommit = True
        started_at = time.perf_counter()
        try:
            result = conn.execute(sql, params) if params else conn.execute(sql)
            return PostgresCursor(result).materialize()
        finally:
            record_query(time.perf_counter() - started_at)
            # Outside IDLE the connection is broken and the pool discards it;
            # changing the mode there would raise over the original error.
            if conn.info.transaction_status == TransactionStatus.IDLE:
                conn.autocommit = False


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
        self._deadline = TransactionDeadline(conn)
        # Tables written so far, or None once a statement's targets are unknown.
        self._written: set[str] | None = set()

    def _note_write(self, sql: str) -> None:
        if self._written is None:
            return
        tables = written_tables(sql)
        if tables is None:
            self._written = None
        else:
            self._written |= tables

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | Mapping[str, Any] = (),
    ) -> Cursor:
        started_at = time.perf_counter()
        self._note_write(sql)
        try:
            self._deadline.prepare()
            # Extended protocol rejects a batch before executing any statement.
            # A pipeline forces it even with prepare_threshold=None and no params;
            # exiting synchronizes the result/error before this operation returns.
            with self._conn.pipeline() if self._deadline.active else nullcontext():
                result = self._conn.execute(sql, params) if params else self._conn.execute(sql)
            self._deadline.observe(sql)
            return PostgresCursor(result)
        finally:
            record_query(time.perf_counter() - started_at)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Cursor:
        self._note_write(sql)
        self._deadline.observe(sql)
        if self._deadline.active:
            # Driver pipelining gives every row the same stale server bound.
            rowcount = 0
            for row in rows:
                rowcount += self.execute(sql, tuple(row)).rowcount
            self._deadline.prepare()
            return PostgresCursor(None, rowcount=rowcount)
        self._deadline.prepare()
        materialized = [tuple(row) for row in rows]
        if not materialized:
            return PostgresCursor(None, rowcount=0)

        driver_executemany = getattr(self._conn, "executemany", None)
        if callable(driver_executemany):
            return PostgresCursor(driver_executemany(sql, materialized))
        with self._conn.cursor() as cursor:
            cursor.executemany(sql, materialized)
            return PostgresCursor(None, rowcount=cursor.rowcount)

    def savepoint(self, name: str) -> Savepoint:
        quoted_name = _quote_identifier(name)
        self.execute(f"SAVEPOINT {quoted_name}")
        return _PostgresSavepoint(self, quoted_name)

    def after_commit(self, callback: Callable[[], None]) -> None:
        if self.closed:
            callback()
            return
        self._after_commit_callbacks.append(callback)

    def acquire_additional_lock(self, lock: LockTarget) -> None:
        if not self.is_immediate:
            raise RuntimeError("additional locks require an immediate transaction")
        if lock in self._locks:
            return
        if isinstance(lock, SessionVariableMutation) and any(
            isinstance(held, SessionVariableTransfer) and lock.session_id in held.session_ids
            for held in self._locks
        ):
            return

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

        for lock_key in advisory_lock_keys(lock):
            self._acquire_advisory_lock(lock_key)

    def _acquire_advisory_lock(self, lock_key: str) -> None:
        self.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))


class PostgresCursor:
    """Cursor adapter whose buffered rows survive returning a connection to the pool.

    It retains normal sequential ``fetchone`` and ``fetchall`` behavior.
    """

    def __init__(self, cursor: Any | None, *, rowcount: int = -1) -> None:
        self._cursor = cursor
        self._rowcount = rowcount
        self._rows: list[Row] | None = None
        self._position = 0
        # psycopg builds a new Column list on every description read, so it is
        # read once per cursor (#23359).
        self._json_column_names: frozenset[str] | None = None

    def materialize(self) -> PostgresCursor:
        if self._cursor is None:
            return self
        description = getattr(self._cursor, "description", None)
        rows: list[Row] = []
        if description is not None:
            self._json_column_names = _json_column_names(description)
            rows = self.fetchall()
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
        return _normalize_row(cast(Row | None, self._cursor.fetchone()), self._json_columns())

    def fetchall(self) -> list[Row]:
        if self._rows is not None:
            rows = self._rows[self._position :]
            self._position = len(self._rows)
            return rows
        if self._cursor is None:
            return []
        json_columns = self._json_columns()
        return [
            row
            for row in (
                _normalize_row(row, json_columns)
                for row in cast(Sequence[Row], self._cursor.fetchall())
            )
            if row is not None
        ]

    def _json_columns(self) -> frozenset[str]:
        if self._json_column_names is None:
            self._json_column_names = _json_column_names(getattr(self._cursor, "description", None))
        return self._json_column_names

    @property
    def rowcount(self) -> int:
        if self._cursor is None:
            return self._rowcount
        return int(getattr(self._cursor, "rowcount", self._rowcount))

    @property
    def lastrowid(self) -> int | None:
        return None


class _PostgresSavepoint:
    def __init__(self, txn: _PostgresTransaction, quoted_name: str) -> None:
        self._txn = txn
        self._quoted_name = quoted_name
        self._deadline_state = txn._deadline.savepoint_state()

    def release(self) -> None:
        self._txn.execute(f"RELEASE SAVEPOINT {self._quoted_name}")

    def rollback(self) -> None:
        # Recovery must remain possible after a server-side statement timeout.
        self._txn._conn.execute(f"ROLLBACK TO SAVEPOINT {self._quoted_name}")
        self._txn._deadline.restore_savepoint_state(self._deadline_state)


def _json_column_names(description: object) -> frozenset[str]:
    if not isinstance(description, list):
        return frozenset()
    # dict_row keeps the last column of a repeated name, so classify the same one.
    is_json = {column.name: column.type_code in _JSON_TYPE_OIDS for column in description}
    return frozenset(name for name, json_typed in is_json.items() if json_typed)


def _normalize_row(row: Row | None, json_columns: frozenset[str] = frozenset()) -> Row | None:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return cast(
            Row,
            {
                str(key): value
                # Most values are plain scalars both branches return unchanged;
                # one exact-type lookup replaces their isinstance chain (#23359).
                if type(value) in _PLAIN_SCALAR_TYPES
                else _dump_json_container(value)
                if key in json_columns
                else _normalize_value(value)
                for key, value in row.items()
            },
        )
    return row


def _dump_json_container(value: Any) -> Any:
    # Storage model decoders consume serialized JSON for both JSONB and text columns.
    # Pool connections' loaders already call this; rows from a connection without
    # them reach it through _normalize_row.
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _normalize_value(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return to_aware_utc(value)
    if isinstance(value, date):
        return value
    if isinstance(value, dict | list):
        return _dump_json_container(to_json_safe(value))
    return value


def conninfo_with_utc_session_timezone(conninfo: str) -> str:
    parsed = conninfo_to_dict(conninfo)
    raw_options = parsed.get("options")
    options = raw_options if isinstance(raw_options, str) else ""
    lower_options = options.lower()
    if "-ctimezone=" not in lower_options and "-c timezone=" not in lower_options:
        parsed["options"] = " ".join(part for part in (options, "-ctimezone=UTC") if part)
    return make_conninfo("", **parsed)


class _JsonContainerTextLoader(TextLoader):
    """Hand JSON objects and arrays on in canonical text; decode JSON scalars.

    Rows carry containers as sorted compact JSON, which stored hashes and edit
    checks compare (stage registry row_hash), so the server's own text will not
    do. Decoded jsonb holds only JSON-native values, so dumping it here skips
    the per-value to_json_safe walk and type dispatch of _normalize_row (#23359).
    """

    def load(self, data: Buffer) -> Any:
        value = json.loads(super().load(data))
        if isinstance(value, dict | list):
            return _dump_json_container(value)
        return value


def configure_pool_connection(
    connection: psycopg.Connection[Any],
    runtime_role: str | None,
) -> None:
    """Install the row loaders on a newly opened pool connection, then its role."""
    adapters = connection.adapters
    adapters.register_loader("json", _JsonContainerTextLoader)
    adapters.register_loader("jsonb", _JsonContainerTextLoader)
    # Rows carry UUIDs as text; PostgreSQL's output is already str(UUID)'s form.
    adapters.register_loader("uuid", TextLoader)
    if runtime_role is not None:
        configure_runtime_role(connection, runtime_role)
        assert_runtime_role(connection, runtime_role)


def check_connection_alive(connection: psycopg.Connection[Any]) -> None:
    """Pool check: reject a pooled connection whose server end went away.

    An idle connection's socket has nothing to read until the server sends a
    FATAL notice or closes it, so a zero-timeout poll finds the connections a
    hub restart killed without a round trip on every checkout (#23359). Only
    a readable socket pays for psycopg_pool's own query check.
    """
    poll = getattr(select, "poll", None)
    if poll is not None:
        readable = poll()
        readable.register(connection.fileno(), select.POLLIN)
        if not readable.poll(0):
            return
    ConnectionPool.check_connection(connection)


def configure_runtime_role(
    connection: psycopg.Connection[Any],
    runtime_role: str,
) -> None:
    """Assume the fixed daemon runtime role on a newly opened pool connection."""
    validate_identifier(runtime_role)
    statement = psycopg_sql.SQL("SET ROLE {}").format(psycopg_sql.Identifier(runtime_role))
    connection.execute(statement)
    connection.commit()


def assert_runtime_role(
    connection: psycopg.Connection[Any],
    runtime_role: str,
) -> None:
    """Reject a connection whose effective identity is not the runtime role.

    The pool runs this once per connection, right after SET ROLE, and
    verify_runtime_identity runs it on demand. It used to run on every
    checkout, which charged a round trip to every database operation (#23359).
    The query itself is unavoidable -- PostgreSQL does not push the role: on
    server 18, parameter_status('role') is None before and after SET ROLE, and
    session_authorization stays at the login user. Autocommit keeps the SELECT
    from opening a transaction that would need a commit to close it (#20853).
    """
    validate_identifier(runtime_role)
    previous_autocommit = connection.autocommit
    connection.autocommit = True
    try:
        row = connection.execute("SELECT current_user").fetchone()
    finally:
        # Restored before any raise below, so the caller and the pool always
        # get the connection back in the mode they handed over.
        connection.autocommit = previous_autocommit
    if isinstance(row, Mapping):
        observed = row.get("current_user")
    else:
        observed = None if row is None else row[0]
    if observed != runtime_role:
        connection.close()
        raise RuntimeRoleMismatchError(
            f"PostgreSQL runtime role mismatch: expected {runtime_role!r}, observed {observed!r}"
        )


def validate_identifier(identifier: str) -> None:
    if not _SQL_IDENTIFIER_PATTERN.fullmatch(identifier):
        raise ValueError(f"invalid SQL identifier: {identifier!r}")


def _quote_identifier(identifier: str) -> str:
    validate_identifier(identifier)
    return f'"{identifier}"'


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


def advisory_lock_keys(lock: LockTarget) -> tuple[str, ...]:
    if isinstance(lock, SessionVariableTransfer):
        return tuple(
            key
            for session_id in sorted(set(lock.session_ids))
            for key in advisory_lock_keys(SessionVariableMutation(session_id))
        )
    if isinstance(lock, MainCheckoutLanding):
        return (f"main_checkout_landing:{lock.project_id}",)
    if isinstance(lock, BuildDryRunMutation):
        return (f"build_dry_run:{lock.project_id}",)
    if isinstance(lock, CronRunAdmission):
        return ("cron_run_admission",)
    if isinstance(lock, AgentCapAdmission):
        return (f"agent_cap_admission:{lock.project_id or '*'}",)
    if isinstance(lock, AgentTaskClaimMutation):
        return (f"agent_task_claim:{lock.session_id}",)
    if isinstance(lock, DispatchMutexRow):
        return (f"dispatch_mutex:{lock.task_id}",)
    if isinstance(lock, ReviewLearningPatternMutation):
        return (f"review_learning_pattern:{lock.project_id}:{lock.pattern_key}",)
    if isinstance(lock, StageReviewApprovalMutation):
        return (f"stage_review_approval:{lock.task_id}",)
    if isinstance(lock, StageReviewRejectionMutation):
        return (f"stage_review_rejection:{lock.task_id}",)
    if isinstance(lock, PlanReviewEvidenceMutation):
        return (f"plan_review_evidence:{lock.project_id}:{lock.plan_path}",)
    if isinstance(lock, IntegrationWorkspaceMutex):
        return (f"integration_workspace_mutex:{lock.integration_key}",)
    if isinstance(lock, IsolationRegistryReconciliation):
        return (f"isolation_registry_reconciliation:{lock.machine_id}",)
    if isinstance(lock, SessionRegistration):
        return (f"session_register:{lock.external_id}|{lock.source}|{lock.session_type}",)
    if isinstance(lock, SessionLineageMutation):
        return ("session_lineage_mutation",)
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
