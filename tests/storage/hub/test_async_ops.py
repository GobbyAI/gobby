from __future__ import annotations

import asyncio
import contextlib
import struct
import threading
import time
from collections.abc import Awaitable, Iterator
from typing import Any

import psutil
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from gobby.storage.hub.async_ops import (
    RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS,
    BoundedDBTimeoutError,
    IndeterminateCommitError,
    run_bounded_db,
)
from tests.fixtures.postgres import isolated_test_schema

pytestmark = pytest.mark.asyncio


@pytest.fixture
def async_ops_schema(postgres_database_url: str) -> Iterator[str]:
    """Dedicated scratch schema for raw-table tests.

    These tests create bare tables with no migrated baseline. Sharing the
    per-worker `postgres_schema` would leave foreign tables in it, which the
    canonical-seed baseline classifier rejects when another suite migrates
    that schema afterwards (bug #18712).
    """
    with isolated_test_schema(postgres_database_url, "asyncops") as schema:
        yield schema


_DEADLINE_SECONDS = RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS + 0.25
_SCHEDULER_TOLERANCE_SECONDS = 0.2
_POSTGRES_CANCEL_REQUEST_CODE = 80877102


class _FakePGConn:
    def __init__(self, activity: list[str]) -> None:
        self._activity = activity
        self.finished = False

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        self._activity.append("hard-close")


class _FakeConnection:
    def __init__(
        self,
        *,
        block_first_set: bool = False,
        block_commit: bool = False,
    ) -> None:
        self.activity: list[str] = []
        self.pgconn = _FakePGConn(self.activity)
        self._block_first_set = block_first_set
        self._block_commit = block_commit
        self._cancel_count = 0
        self.block_entered = asyncio.Event()
        self.commit_entered = asyncio.Event()

    async def execute(self, query: str) -> None:
        self.activity.append(query)
        if self._block_first_set or query == "SELECT blocked":
            self._block_first_set = False
            self.block_entered.set()
            await self._stubborn_wait()

    async def commit(self) -> None:
        self.activity.append("commit")
        self.commit_entered.set()
        if self._block_commit:
            await self._stubborn_wait()

    async def rollback(self) -> None:
        self.activity.append("rollback")

    async def close(self) -> None:
        self.activity.append("close")
        self.pgconn.finish()

    async def _stubborn_wait(self) -> None:
        never = asyncio.Event()
        while True:
            try:
                await never.wait()
            except asyncio.CancelledError:
                self._cancel_count += 1
                self.activity.append(f"cancel-{self._cancel_count}")
                if self._cancel_count >= 2:
                    raise


async def _assert_bounded_timeout(
    call: Awaitable[Any],
    *,
    deadline_seconds: float = _DEADLINE_SECONDS,
) -> None:
    sentinel = asyncio.create_task(asyncio.sleep(0.05))
    started = time.monotonic()
    with pytest.raises(BoundedDBTimeoutError):
        await call
    elapsed = time.monotonic() - started

    assert elapsed <= deadline_seconds + _SCHEDULER_TOLERANCE_SECONDS
    assert sentinel.done(), "event loop stopped making progress during bounded DB work"


async def _wait_for_event_loop_callback(delay: float = 0.0) -> None:
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    if delay > 0.0:
        loop.call_later(delay, completed.set_result, None)
    else:
        loop.call_soon(completed.set_result, None)
    await completed


def _scoped_conninfo(database_url: str, schema: str) -> str:
    params = _string_conn_params(database_url)
    if params.get("host") in {"localhost", "127.0.0.1"}:
        params["hostaddr"] = "127.0.0.1"
    existing_options = params.get("options", "")
    params["options"] = f"{existing_options} -csearch_path={schema}".strip()
    return make_conninfo("", **params)


def _string_conn_params(conninfo: str) -> dict[str, str]:
    return {
        key: str(value) for key, value in conninfo_to_dict(conninfo).items() if value is not None
    }


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


class _PostgresProxy:
    """Small plaintext proxy for exercising PostgreSQL cancellation boundaries."""

    def __init__(
        self,
        upstream_conninfo: str,
        *,
        block_cancel: bool = False,
        drop_commit_response: bool = False,
    ) -> None:
        params = _string_conn_params(upstream_conninfo)
        upstream_host = params.get("hostaddr") or params.get("host", "localhost")
        self._upstream_host = upstream_host.split(",", maxsplit=1)[0]
        if self._upstream_host == "localhost":
            self._upstream_host = "127.0.0.1"
        self._upstream_port = int(params.get("port", "5432").split(",", maxsplit=1)[0])
        self._upstream_conninfo = upstream_conninfo
        self._block_cancel = block_cancel
        self._drop_commit_response = drop_commit_response
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._handler_errors: list[BaseException] = []
        self._release = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.commit_forwarded = asyncio.Event()
        self.conninfo = ""

    async def __aenter__(self) -> _PostgresProxy:
        self._server = await asyncio.start_server(self._start_handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.conninfo = make_conninfo(
            self._upstream_conninfo,
            host="127.0.0.1",
            hostaddr="127.0.0.1",
            port=port,
            sslmode="disable",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: Any,
    ) -> None:
        del exc, traceback
        self._release.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        handlers = tuple(self._handlers)
        for handler in handlers:
            handler.cancel()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        if exc_type is None and self._handler_errors:
            raise AssertionError("PostgreSQL proxy handler failed") from self._handler_errors[0]

    def _start_handler(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handler_done)

    def _handler_done(self, task: asyncio.Task[None]) -> None:
        self._handlers.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._handler_errors.append(error)

    async def _handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        pipe_tasks: set[asyncio.Task[None]] = set()
        try:
            header = await client_reader.readexactly(8)
            length, request_code = struct.unpack("!II", header)
            payload = await client_reader.readexactly(length - len(header))
            if request_code == _POSTGRES_CANCEL_REQUEST_CODE:
                self.cancel_seen.set()
                if self._block_cancel:
                    await self._release.wait()
                    return
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    self._upstream_host,
                    self._upstream_port,
                )
                upstream_writer.write(header + payload)
                await upstream_writer.drain()
                await upstream_reader.read()
                return

            upstream_reader, upstream_writer = await asyncio.open_connection(
                self._upstream_host,
                self._upstream_port,
            )
            upstream_writer.write(header + payload)
            await upstream_writer.drain()
            drop_responses = asyncio.Event()
            client_to_upstream = asyncio.create_task(
                self._client_to_upstream(
                    client_reader,
                    upstream_writer,
                    drop_responses,
                )
            )
            upstream_to_client = asyncio.create_task(
                self._upstream_to_client(
                    upstream_reader,
                    client_writer,
                    drop_responses,
                )
            )
            pipe_tasks = {client_to_upstream, upstream_to_client}
            done, pending = await asyncio.wait(
                pipe_tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            for task in pipe_tasks:
                task.cancel()
            if pipe_tasks:
                await asyncio.gather(*pipe_tasks, return_exceptions=True)
            await _close_writer(client_writer)
            if upstream_writer is not None:
                await _close_writer(upstream_writer)

    async def _client_to_upstream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        drop_responses: asyncio.Event,
    ) -> None:
        tail = b""
        while data := await reader.read(65536):
            observed = tail + data
            if self._drop_commit_response and b"COMMIT\x00" in observed:
                self.commit_forwarded.set()
                drop_responses.set()
            writer.write(data)
            await writer.drain()
            tail = observed[-16:]

    async def _upstream_to_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        drop_responses: asyncio.Event,
    ) -> None:
        while data := await reader.read(65536):
            if drop_responses.is_set():
                continue
            writer.write(data)
            await writer.drain()


def _install_fake_connect(
    monkeypatch: pytest.MonkeyPatch,
    connection: _FakeConnection,
) -> None:
    async def connect(*_args: Any, **_kwargs: Any) -> _FakeConnection:
        return connection

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)


async def test_termination_matrix(
    monkeypatch: pytest.MonkeyPatch,
    postgres_database_url: str,
    async_ops_schema: str,
) -> None:
    """Every pre-commit wait site terminates inside the caller's original deadline."""
    threads_before = {thread.ident for thread in threading.enumerate()}
    accepted = asyncio.Event()
    release_server = asyncio.Event()

    async def silent_handler(
        _reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        accepted.set()
        await release_server.wait()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(silent_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    blocked_conninfo = make_conninfo(
        host="127.0.0.1",
        port=port,
        user="gobby",
        dbname="gobby",
        sslmode="disable",
    )

    async def unused_work(_conn: Any, _remaining: float) -> None:
        raise AssertionError("work ran after a blocked connect")

    try:
        await _assert_bounded_timeout(
            run_bounded_db(
                unused_work,
                conninfo=blocked_conninfo,
                deadline_seconds=_DEADLINE_SECONDS,
            )
        )
        assert accepted.is_set()
    finally:
        release_server.set()
        server.close()
        await server.wait_closed()

    cases = (("first SET LOCAL", True), ("server-side statement", False))
    for label, block_first_set in cases:
        connection = _FakeConnection(block_first_set=block_first_set)
        with monkeypatch.context() as fake_patch:
            _install_fake_connect(fake_patch, connection)

            async def blocked_work(conn: Any, _remaining: float) -> None:
                await conn.execute("SELECT blocked")

            await _assert_bounded_timeout(
                run_bounded_db(
                    blocked_work,
                    conninfo="postgresql://unused",
                    deadline_seconds=_DEADLINE_SECONDS,
                )
            )
        assert connection.block_entered.is_set(), label
        assert connection.activity.count("cancel-1") == 1, label
        assert connection.activity.count("cancel-2") == 1, label
        assert connection.activity.count("hard-close") == 1, label
        activity_at_return = list(connection.activity)
        await _wait_for_event_loop_callback(0.02)
        assert connection.activity == activity_at_return, label

    conninfo = _scoped_conninfo(postgres_database_url, async_ops_schema)
    table = "bounded_async_termination_matrix"
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as setup:
        await setup.execute(f"DROP TABLE IF EXISTS {table}")
        await setup.execute(f"CREATE TABLE {table} (id integer PRIMARY KEY)")
        await setup.execute(f"INSERT INTO {table} VALUES (1)")

    holder = await psycopg.AsyncConnection.connect(conninfo)
    await holder.execute(f"SELECT id FROM {table} WHERE id = 1 FOR UPDATE")
    try:

        async def wait_for_foreign_lock(conn: Any, _remaining: float) -> None:
            await conn.execute(f"SELECT id FROM {table} WHERE id = 1 FOR UPDATE")

        await _assert_bounded_timeout(
            run_bounded_db(
                wait_for_foreign_lock,
                conninfo=conninfo,
                deadline_seconds=_DEADLINE_SECONDS,
                statement_timeout_remaining=False,
                lock_timeout=True,
            )
        )
    finally:
        await holder.rollback()
        await holder.close()

    async with _PostgresProxy(conninfo, block_cancel=True) as proxy:

        async def wait_on_server(conn: Any, _remaining: float) -> None:
            await conn.execute("SET LOCAL statement_timeout = 275")
            await conn.execute("SELECT pg_sleep(10)")

        await _assert_bounded_timeout(
            run_bounded_db(
                wait_on_server,
                conninfo=proxy.conninfo,
                deadline_seconds=_DEADLINE_SECONDS,
                statement_timeout_remaining=False,
            )
        )
        assert proxy.cancel_seen.is_set()

    assert {thread.ident for thread in threading.enumerate()} == threads_before


async def test_repeated_timeouts_leave_stable_tasks_threads_and_fds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = asyncio.current_task()
    tasks_before = {task for task in asyncio.all_tasks() if task is not current}
    threads_before = {thread.ident for thread in threading.enumerate()}
    fds_before = psutil.Process().num_fds()

    for _ in range(4):
        connection = _FakeConnection()
        _install_fake_connect(monkeypatch, connection)

        async def blocked_work(conn: Any, _remaining: float) -> None:
            await conn.execute("SELECT blocked")

        await _assert_bounded_timeout(
            run_bounded_db(
                blocked_work,
                conninfo="postgresql://unused",
                deadline_seconds=_DEADLINE_SECONDS,
            )
        )

    await _wait_for_event_loop_callback()
    assert {task for task in asyncio.all_tasks() if task is not current} == tasks_before
    assert {thread.ident for thread in threading.enumerate()} == threads_before
    assert psutil.Process().num_fds() == fds_before


async def test_supervisor_cancellation_reaps_precommit_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    _install_fake_connect(monkeypatch, connection)

    async def blocked_work(conn: Any, _remaining: float) -> None:
        await conn.execute("SELECT blocked")

    supervisor = asyncio.create_task(
        run_bounded_db(
            blocked_work,
            conninfo="postgresql://unused",
            deadline_seconds=RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS + 2.0,
        )
    )
    await connection.block_entered.wait()
    supervisor.cancel()

    with pytest.raises(asyncio.CancelledError):
        await supervisor
    assert connection.activity.count("cancel-1") == 1
    assert connection.activity.count("cancel-2") == 1
    assert connection.activity.count("hard-close") == 1


async def test_supervisor_cancellation_during_commit_is_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(block_commit=True)
    _install_fake_connect(monkeypatch, connection)

    async def completed_work(_conn: Any, _remaining: float) -> None:
        return None

    supervisor = asyncio.create_task(
        run_bounded_db(
            completed_work,
            conninfo="postgresql://unused",
            deadline_seconds=RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS + 2.0,
        )
    )
    await connection.commit_entered.wait()
    supervisor.cancel()

    with pytest.raises(IndeterminateCommitError):
        await supervisor
    assert connection.activity.count("cancel-1") == 1
    assert connection.activity.count("cancel-2") == 1
    assert connection.activity.count("hard-close") == 1


async def test_lock_release_on_hard_close(
    monkeypatch: pytest.MonkeyPatch,
    postgres_database_url: str,
    async_ops_schema: str,
) -> None:
    conninfo = _scoped_conninfo(postgres_database_url, async_ops_schema)
    table = "bounded_async_lock_release"
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as setup:
        await setup.execute(f"DROP TABLE IF EXISTS {table}")
        await setup.execute(f"CREATE TABLE {table} (id integer PRIMARY KEY, value integer)")
        await setup.execute(f"INSERT INTO {table} VALUES (1, 0)")

    cancel_started = asyncio.Event()
    original_cancel_safe = psycopg.AsyncConnection.cancel_safe

    async def silent_cancel(self: Any, *, timeout: float = 5.0) -> None:
        del self, timeout
        cancel_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(psycopg.AsyncConnection, "cancel_safe", silent_cancel)

    async def hold_lock(conn: Any, _remaining: float) -> None:
        await conn.execute("SET LOCAL statement_timeout = 600")
        await conn.execute(f"SELECT id FROM {table} WHERE id = 1 FOR UPDATE")
        await conn.execute("SELECT pg_sleep(10)")

    await _assert_bounded_timeout(
        run_bounded_db(
            hold_lock,
            conninfo=conninfo,
            deadline_seconds=_DEADLINE_SECONDS,
            statement_timeout_remaining=False,
        )
    )
    assert cancel_started.is_set()
    monkeypatch.setattr(psycopg.AsyncConnection, "cancel_safe", original_cancel_safe)

    async with asyncio.timeout(0.75):
        async with await psycopg.AsyncConnection.connect(conninfo) as verifier:
            await verifier.execute(f"SELECT id FROM {table} WHERE id = 1 FOR UPDATE")


async def test_commit_phase_outcomes(
    postgres_database_url: str,
    async_ops_schema: str,
) -> None:
    conninfo = _scoped_conninfo(postgres_database_url, async_ops_schema)
    table = "bounded_async_commit_outcomes"
    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as setup:
        await setup.execute(f"DROP TABLE IF EXISTS {table}")
        await setup.execute(f"CREATE TABLE {table} (id integer PRIMARY KEY, value text)")

    async def insert_committed(conn: Any, _remaining: float) -> str:
        await conn.execute(f"INSERT INTO {table} VALUES (1, 'committed')")
        return "done"

    assert (
        await run_bounded_db(
            insert_committed,
            conninfo=conninfo,
            deadline_seconds=RUN_BOUNDED_DB_CLEANUP_SLICE_SECONDS + 1.5,
        )
        == "done"
    )

    async def insert_then_expire(conn: Any, _remaining: float) -> None:
        await conn.execute(f"INSERT INTO {table} VALUES (2, 'rolled-back')")
        await asyncio.Event().wait()

    await _assert_bounded_timeout(
        run_bounded_db(
            insert_then_expire,
            conninfo=conninfo,
            deadline_seconds=_DEADLINE_SECONDS,
        )
    )

    async def insert_indeterminate(conn: Any, _remaining: float) -> None:
        await conn.execute(f"INSERT INTO {table} VALUES (3, 'indeterminate')")

    async with _PostgresProxy(conninfo, drop_commit_response=True) as proxy:
        started = time.monotonic()
        with pytest.raises(IndeterminateCommitError):
            await run_bounded_db(
                insert_indeterminate,
                conninfo=proxy.conninfo,
                deadline_seconds=_DEADLINE_SECONDS,
            )
        elapsed = time.monotonic() - started
        assert elapsed <= _DEADLINE_SECONDS + _SCHEDULER_TOLERANCE_SECONDS
        assert proxy.commit_forwarded.is_set()

    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as verifier:
        rows = await (
            await verifier.execute(f"SELECT id, value FROM {table} ORDER BY id")
        ).fetchall()

    assert rows == [(1, "committed"), (3, "indeterminate")]
