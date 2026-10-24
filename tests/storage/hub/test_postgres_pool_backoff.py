from __future__ import annotations

import importlib
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool, PoolTimeout

from gobby.config.postgres_pool import PostgresPoolConfig, postgres_pool_config_from_mapping
from gobby.storage.hub import operation_deadline, pool_census, postgres_pool
from gobby.storage.hub.async_ops import IndeterminateCommitError
from gobby.storage.hub.operation_deadline import database_operation_deadline

pytestmark = pytest.mark.unit


class _TimeoutContext:
    def __init__(self, raised: list[PoolTimeout] | None = None) -> None:
        self._raised = raised

    def __enter__(self) -> Any:
        error = PoolTimeout("couldn't get a connection after 5.00 sec")
        if self._raised is not None:
            self._raised.append(error)
        raise error

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _ConnectionContext:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    def __enter__(self) -> Any:
        return self._conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakePool:
    def __init__(self, failures: int) -> None:
        self.failures_remaining = failures
        self.connection_calls = 0
        self.check_calls = 0
        self.connection_timeouts: list[float | None] = []
        self.conninfo = _CENSUS_CONNINFO
        self.raised: list[PoolTimeout] = []

    def connection(self, timeout: float | None = None) -> Any:
        self.connection_calls += 1
        self.connection_timeouts.append(timeout)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            return _TimeoutContext(self.raised)
        return _ConnectionContext(object())

    def check(self) -> None:
        self.check_calls += 1


def _pool_stats() -> dict[str, int]:
    return {"pool_size": 20, "pool_available": 0}


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return sleeps


def test_pool_connection_recovers_after_backoff_retries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = _FakePool(failures=3)
    sleeps = _patch_sleep(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=postgres_pool.__name__):
        with postgres_pool.pool_connection(cast(ConnectionPool[Any], pool), _pool_stats) as conn:
            assert conn is not None

    assert pool.connection_calls == 4
    assert pool.check_calls == 3
    assert len(sleeps) == 3
    for sleep, base in zip(sleeps, postgres_pool.POOL_TIMEOUT_RETRY_BACKOFF_SECONDS, strict=True):
        assert base <= sleep <= base * (1 + postgres_pool.POOL_TIMEOUT_RETRY_JITTER_RATIO)
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []


def test_pool_connection_logs_single_error_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pool = _FakePool(failures=99)
    sleeps = _patch_sleep(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=postgres_pool.__name__):
        with pytest.raises(PoolTimeout):
            with postgres_pool.pool_connection(cast(ConnectionPool[Any], pool), _pool_stats):
                pass

    attempts = len(postgres_pool.POOL_TIMEOUT_RETRY_BACKOFF_SECONDS)
    assert pool.connection_calls == attempts + 1
    assert pool.check_calls == attempts
    assert len(sleeps) == attempts

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert "failed after" in error_records[0].getMessage()
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records == []


def test_pool_connection_checks_pool_before_each_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class OrderedPool:
        def __init__(self) -> None:
            self.failures = 1

        def connection(self) -> Any:
            events.append("connection")
            if self.failures > 0:
                self.failures -= 1
                return _TimeoutContext()
            return _ConnectionContext(object())

        def check(self) -> None:
            events.append("check")

    pool = OrderedPool()
    _patch_sleep(monkeypatch)

    with postgres_pool.pool_connection(cast(ConnectionPool[Any], pool), _pool_stats):
        pass

    assert events == ["connection", "check", "connection"]


def test_pool_connection_honors_operation_deadline_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The deadline reads a clock that never advances, so the 30 ms budget cannot
    # expire while the test is descheduled under parallel load (#23359).
    monkeypatch.setattr(operation_deadline, "time", SimpleNamespace(monotonic=lambda: 0.0))
    pool = _FakePool(failures=1)

    with database_operation_deadline(timeout_seconds=0.03):
        with pytest.raises(PoolTimeout):
            with postgres_pool.pool_connection(cast(ConnectionPool[Any], pool), _pool_stats):
                pass

    assert pool.connection_calls == 1
    assert pool.check_calls == 0
    assert len(pool.connection_timeouts) == 1
    timeout = pool.connection_timeouts[0]
    assert timeout is not None
    assert 0 < timeout <= 0.03


_CENSUS_SECRET = "census-sentinel-secret"
_CENSUS_CONNINFO = f"host=census.invalid dbname=gobby user=gobby password={_CENSUS_SECRET}"
_CENSUS_DSN = f"postgresql://gobby:{_CENSUS_SECRET}@census.invalid:5432/gobby"
_CENSUS_ROWS = [("gobby-hub-abc", "active", 3), ("gobby-hub-abc", "idle", 7)]


class _CensusServer:
    """Stands in for psycopg.connect and records each census connection."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, dict[str, Any]]] = []
        self.queries: list[str] = []
        self.closed = 0
        self.connect_error: Exception | None = None
        self.query_error: Exception | None = None
        self.query_entered = threading.Event()
        self.query_release = threading.Event()
        self.query_release.set()

    def connect(self, conninfo: str, **kwargs: Any) -> _CensusConnection:
        self.connects.append((conninfo, kwargs))
        if self.connect_error is not None:
            raise self.connect_error
        return _CensusConnection(self)


class _CensusConnection:
    def __init__(self, server: _CensusServer) -> None:
        self._server = server

    def execute(self, query: str) -> _CensusConnection:
        self._server.queries.append(query)
        self._server.query_entered.set()
        self._server.query_release.wait(timeout=5)
        if self._server.query_error is not None:
            raise self._server.query_error
        return self

    def fetchall(self) -> list[tuple[str, str, int]]:
        return list(_CENSUS_ROWS)

    def close(self) -> None:
        self._server.closed += 1


def _join_census_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == pool_census.ACTIVITY_CENSUS_THREAD_NAME:
            thread.join(timeout=5)


@pytest.fixture(autouse=True)
def census_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_CensusServer]:
    server = _CensusServer()
    monkeypatch.setattr(pool_census, "_ACTIVITY_CENSUS", pool_census._ActivityCensusGate())
    monkeypatch.setattr(psycopg, "connect", server.connect)
    yield server
    server.query_release.set()
    _join_census_threads()


def _fail_acquire(pool: _FakePool) -> PoolTimeout:
    with pytest.raises(PoolTimeout) as raised:
        with postgres_pool.pool_connection(cast(ConnectionPool[Any], pool), _pool_stats):
            pass
    return raised.value


def _rewind_census_clock() -> None:
    gate = pool_census._ACTIVITY_CENSUS
    assert gate.last_started_at is not None
    gate.last_started_at -= pool_census.ACTIVITY_CENSUS_INTERVAL_SECONDS + 1


def _census_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "activity census" in r.getMessage()]


def test_final_acquire_failure_logs_a_rate_limited_activity_census(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    census_server: _CensusServer,
) -> None:
    _patch_sleep(monkeypatch)

    with caplog.at_level(logging.INFO, logger=pool_census.__name__):
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(_fail_acquire, [_FakePool(failures=99) for _ in range(4)]))
        _join_census_threads()
        _fail_acquire(_FakePool(failures=99))
        _join_census_threads()

    assert len(census_server.connects) == 1
    assert census_server.queries == [
        "SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1, 2"
    ]
    assert census_server.closed == 1
    records = _census_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert str(_pool_stats()) in message
    assert str(_CENSUS_ROWS) in message

    _rewind_census_clock()
    _fail_acquire(_FakePool(failures=99))
    _join_census_threads()

    assert len(census_server.connects) == 2


@pytest.mark.parametrize(
    ("error_attr", "error", "capacity_claimed"),
    [
        (
            "connect_error",
            psycopg.errors.TooManyConnections(
                f"connection to {_CENSUS_DSN} failed: sorry, too many clients already"
            ),
            True,
        ),
        (
            "connect_error",
            psycopg.errors.InvalidPassword(
                f"connection to {_CENSUS_DSN} failed: password authentication failed"
            ),
            False,
        ),
        (
            "connect_error",
            psycopg.errors.InvalidAuthorizationSpecification(
                f"connection to {_CENSUS_DSN} failed: no pg_hba.conf entry"
            ),
            False,
        ),
        (
            "connect_error",
            psycopg.errors.CannotConnectNow(
                f"connection to {_CENSUS_DSN} failed: the database system is starting up"
            ),
            False,
        ),
        (
            "query_error",
            psycopg.errors.QueryCanceled(
                f"canceling statement due to statement timeout on {_CENSUS_DSN}"
            ),
            False,
        ),
        (
            "connect_error",
            psycopg.OperationalError(f"connection to {_CENSUS_DSN} failed: Connection refused"),
            False,
        ),
    ],
    ids=["53300", "28P01", "28000", "57P03", "57014", "no-sqlstate"],
)
def test_census_failure_reason_is_typed_and_claims_capacity_only_for_53300(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    census_server: _CensusServer,
    error_attr: str,
    error: psycopg.Error,
    capacity_claimed: bool,
) -> None:
    _patch_sleep(monkeypatch)
    monkeypatch.setattr(census_server, error_attr, error)

    with caplog.at_level(logging.INFO, logger=pool_census.__name__):
        _fail_acquire(_FakePool(failures=99))
        _join_census_threads()

    records = _census_records(caplog)
    assert len(records) == 1
    record = records[0]
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert f"error={type(error).__name__}" in message
    assert f"sqlstate={error.sqlstate}" in message
    assert ("capacity exhausted" in message) is capacity_claimed
    assert record.exc_info is None
    assert _CENSUS_SECRET not in caplog.text
    assert "census.invalid" not in caplog.text


def test_census_is_bounded_and_preserves_the_original_acquisition_error(
    monkeypatch: pytest.MonkeyPatch,
    census_server: _CensusServer,
) -> None:
    _patch_sleep(monkeypatch)
    census_server.query_release.clear()

    stalled_pool = _FakePool(failures=99)
    started = time.monotonic()
    stalled_error = _fail_acquire(stalled_pool)
    elapsed = time.monotonic() - started

    assert census_server.query_entered.wait(timeout=5)
    assert stalled_error is stalled_pool.raised[0]
    assert stalled_error.__cause__ is None
    assert elapsed < 2.0

    _rewind_census_clock()
    blocked_pool = _FakePool(failures=99)
    assert _fail_acquire(blocked_pool) is blocked_pool.raised[0]
    assert len(census_server.connects) == 1
    assert census_server.connects[0] == (
        _CENSUS_CONNINFO,
        {
            "application_name": pool_census.ACTIVITY_CENSUS_THREAD_NAME,
            "connect_timeout": 2,
            "options": "-c statement_timeout=2000",
            "keepalives": 1,
            "keepalives_idle": 5,
            "keepalives_interval": 2,
            "keepalives_count": 2,
        },
    )

    census_server.query_release.set()
    _join_census_threads()
    assert census_server.closed == 1

    census_server.connect_error = psycopg.OperationalError(f"connection to {_CENSUS_DSN} failed")
    _rewind_census_clock()
    failed_census_pool = _FakePool(failures=99)
    failed_census_error = _fail_acquire(failed_census_pool)
    _join_census_threads()
    assert failed_census_error is failed_census_pool.raised[0]
    assert failed_census_error.__cause__ is None

    census_server.connect_error = None
    _rewind_census_clock()
    census_ok_pool = _FakePool(failures=99)
    assert _fail_acquire(census_ok_pool) is census_ok_pool.raised[0]
    _join_census_threads()
    assert len(census_server.connects) == 3


class _TransactionConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[str, ...]]] = []
        self.info = SimpleNamespace(transaction_status=TransactionStatus.INTRANS)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def execute(self, sql: str, params: tuple[str, ...] = ()) -> _TransactionConnection:
        self.statements.append((sql, params))
        return self

    def fetchone(self) -> dict[str, str]:
        return {"statement_timeout": "0", "lock_timeout": "0"}


def test_transaction_context_applies_operation_deadline_as_local_bounds() -> None:
    conn = _TransactionConnection()

    @contextmanager
    def connection() -> Iterator[Any]:
        yield conn

    with database_operation_deadline(timeout_seconds=0.25):
        with postgres_pool.transaction_context(
            lambda: None,
            connection,
            is_immediate=False,
        ):
            pass

    assert len(conn.statements) == 3
    sql, params = conn.statements[2]
    assert params == ()
    statement, lock = sql.split("; ")
    assert statement.startswith("SET LOCAL statement_timeout = '")
    assert lock.startswith("SET LOCAL lock_timeout = '")
    for setting in (statement, lock):
        value = setting.split("'")[1]
        assert 0 < int(value.removesuffix("ms")) <= 250


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("commit outcome unavailable"),
        psycopg.OperationalError("connection lost"),
        psycopg.errors.QueryCanceled("commit cancelled"),
        psycopg.errors.LockNotAvailable("commit lock timed out"),
        psycopg.errors.StatementCompletionUnknown("commit outcome unknown"),
    ],
)
def test_transaction_context_reports_unobserved_commit_as_indeterminate(
    error: BaseException,
) -> None:
    class CommitFailureConnection(_TransactionConnection):
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield
            raise error

    conn = CommitFailureConnection()

    @contextmanager
    def connection() -> Iterator[Any]:
        yield conn

    with pytest.raises(IndeterminateCommitError) as raised:
        with postgres_pool.transaction_context(
            lambda: None,
            connection,
            is_immediate=False,
        ):
            pass

    assert raised.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.UniqueViolation("deferred uniqueness violation"),
        psycopg.errors.SerializationFailure("serialization rejected"),
        psycopg.errors.DeadlockDetected("deadlock rejected"),
        psycopg.errors.RaiseException(
            "deferred trigger rejected commit",
            info={psycopg.pq.DiagnosticField.SEVERITY_NONLOCALIZED: b"ERROR"},
        ),
    ],
)
def test_transaction_context_preserves_definite_commit_rejections(error: psycopg.Error) -> None:
    class CommitFailureConnection(_TransactionConnection):
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield
            raise error

    conn = CommitFailureConnection()

    @contextmanager
    def connection() -> Iterator[Any]:
        yield conn

    with pytest.raises(type(error)) as raised:
        with postgres_pool.transaction_context(lambda: None, connection, is_immediate=False):
            pass

    assert raised.value is error


def _postgres_module() -> ModuleType:
    return importlib.import_module("gobby.storage.hub.postgres")


def test_hub_pool_uses_max_lifetime_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _postgres_module()
    calls: dict[str, Any] = {}

    class FakePool:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls.update(kwargs)

        def open(self, *, wait: bool, timeout: float) -> None:
            return None

        def close(self, *, timeout: float | None = None) -> None:
            return None

    monkeypatch.setattr(module, "ConnectionPool", FakePool)
    database = module.PostgresHubDatabase(
        "postgresql://gobby:secret@localhost/gobby",
        pool_config=PostgresPoolConfig(max_lifetime_seconds=120.0),
    )

    assert calls["max_lifetime"] == 120.0
    database.close()


def test_postgres_pool_config_defaults_max_lifetime() -> None:
    config = PostgresPoolConfig()
    assert config.max_lifetime_seconds == 300.0
    assert config.to_dict()["max_lifetime_seconds"] == 300.0


def test_postgres_pool_config_from_mapping_reads_max_lifetime() -> None:
    config = postgres_pool_config_from_mapping({"max_lifetime_seconds": 600})
    assert config.max_lifetime_seconds == 600.0


def test_postgres_pool_config_rejects_invalid_max_lifetime() -> None:
    with pytest.raises(ValueError, match="max_lifetime_seconds"):
        PostgresPoolConfig(max_lifetime_seconds=0)
    with pytest.raises(ValueError, match="max_lifetime_seconds"):
        postgres_pool_config_from_mapping({"max_lifetime_seconds": "five"})
