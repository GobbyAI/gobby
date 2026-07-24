from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from types import ModuleType
from typing import ClassVar

import pytest

from gobby.build import lifecycle as build_lifecycle
from gobby.build.options import BuildOptions
from gobby.build.results import BuildResult
from gobby.config.postgres_pool import PostgresPoolConfig

pytestmark = pytest.mark.unit


def _postgres_module():
    return importlib.import_module("gobby.storage.hub.postgres")


def _postgres_pool_module() -> ModuleType:
    return importlib.import_module("gobby.storage.hub.postgres_pool")


def test_stage_review_approval_lock_key_is_task_scoped() -> None:
    from gobby.storage.hub.protocol import StageReviewApprovalMutation

    pool_module = _postgres_pool_module()

    assert pool_module._advisory_lock_keys(StageReviewApprovalMutation(task_id="task-1")) == (
        "stage_review_approval:task-1",
    )


def test_postgres_transaction_is_closed_when_context_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()

    class Connection:
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield

    connection = Connection()
    database = object.__new__(module.PostgresHubDatabase)
    monkeypatch.setattr(database, "open", lambda: None)

    @contextmanager
    def pool_connection() -> Iterator[Connection]:
        yield connection

    monkeypatch.setattr(database, "_pool_connection", pool_connection)

    with database._transaction_context(is_immediate=False) as transaction:
        assert transaction.closed is False

    assert transaction.closed is True


def test_postgres_execute_materializes_results_before_transaction_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()
    pool_module = _postgres_pool_module()
    checkout_active = True

    class Result:
        description = object()

        @property
        def rowcount(self) -> int:
            assert checkout_active
            return 2

        def fetchone(self):
            assert checkout_active
            return {"id": 1}

        def fetchall(self):
            assert checkout_active
            return [{"id": 1}, {"id": 2}]

    class Transaction:
        def execute(self, sql: str, params: object = ()):
            return pool_module._PostgresCursor(Result())

    @contextmanager
    def transaction() -> Iterator[Transaction]:
        nonlocal checkout_active
        try:
            yield Transaction()
        finally:
            checkout_active = False

    database = object.__new__(module.PostgresHubDatabase)
    monkeypatch.setattr(database, "transaction", transaction)

    cursor = database.execute("SELECT id FROM tasks")

    assert cursor.rowcount == 2
    assert cursor.fetchone() == {"id": 1}
    assert cursor.fetchall() == [{"id": 2}]


def test_postgres_after_commit_callback_failures_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    module = _postgres_module()
    pool_module = _postgres_pool_module()

    class Connection:
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield

    database = object.__new__(module.PostgresHubDatabase)
    monkeypatch.setattr(database, "open", lambda: None)

    @contextmanager
    def pool_connection() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(database, "_pool_connection", pool_connection)
    callbacks_run: list[str] = []

    def failing_callback() -> None:
        callbacks_run.append("failing")
        raise RuntimeError("callback failed")

    def succeeding_callback() -> None:
        callbacks_run.append("succeeding")

    with caplog.at_level(logging.ERROR, logger=pool_module.__name__):
        with database._transaction_context(is_immediate=False) as transaction:
            transaction.after_commit(failing_callback)
            transaction.after_commit(succeeding_callback)

    assert callbacks_run == ["failing", "succeeding"]
    record = next(
        record
        for record in caplog.records
        if record.getMessage() == "PostgreSQL after-commit callback failed"
    )
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


def test_nested_native_transaction_callbacks_run_on_their_own_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()

    class Connection:
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield

    database = object.__new__(module.PostgresHubDatabase)
    monkeypatch.setattr(database, "open", lambda: None)

    @contextmanager
    def pool_connection() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(database, "_pool_connection", pool_connection)
    callbacks_run: list[str] = []

    with database._transaction_context(is_immediate=False) as outer:
        outer.after_commit(lambda: callbacks_run.append("outer"))
        with database._transaction_context(is_immediate=False) as inner:
            inner.after_commit(lambda: callbacks_run.append("inner"))
        assert callbacks_run == ["inner"]

    assert callbacks_run == ["inner", "outer"]


@pytest.mark.asyncio
async def test_interleaved_transactions_own_callbacks_and_lock_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()

    @dataclass(frozen=True)
    class Lock:
        PRIORITY: ClassVar[int]
        name: str

    @dataclass(frozen=True)
    class OuterLock(Lock):
        PRIORITY: ClassVar[int] = 20

    @dataclass(frozen=True)
    class IndependentLock(Lock):
        PRIORITY: ClassVar[int] = 10

    class Connection:
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield

        def execute(self, sql: str, params: object = ()) -> object:
            return object()

    database = object.__new__(module.PostgresHubDatabase)
    monkeypatch.setattr(database, "open", lambda: None)

    @contextmanager
    def pool_connection() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(database, "_pool_connection", pool_connection)
    first_entered = asyncio.Event()
    second_committed = asyncio.Event()
    callbacks_run: list[str] = []

    async def first_transaction() -> None:
        with database.transaction_immediate(OuterLock("outer")):
            database.after_commit(lambda: callbacks_run.append("first"))
            first_entered.set()
            await second_committed.wait()
            assert callbacks_run == ["second"]

    async def second_transaction() -> None:
        await first_entered.wait()
        with database.transaction_immediate(IndependentLock("independent")):
            database.after_commit(lambda: callbacks_run.append("second"))
        second_committed.set()

    await asyncio.gather(first_transaction(), second_transaction())

    assert callbacks_run == ["second", "first"]


@pytest.mark.asyncio
async def test_build_dry_run_interleaving_keeps_transaction_locks_task_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()

    class Connection:
        @contextmanager
        def transaction(self) -> Iterator[None]:
            yield

        def execute(self, sql: str, params: object = ()) -> object:
            return object()

    database = object.__new__(module.PostgresHubDatabase)
    monkeypatch.setattr(database, "open", lambda: None)

    @contextmanager
    def pool_connection() -> Iterator[Connection]:
        yield Connection()

    monkeypatch.setattr(database, "_pool_connection", pool_connection)
    both_builds_entered = asyncio.Event()
    entered_count = 0

    async def build_impl(
        input_ref: str,
        opts: BuildOptions,
        *,
        db: object,
        project_id: str,
        services: object | None,
    ) -> BuildResult:
        nonlocal entered_count
        entered_count += 1
        if entered_count == 2:
            both_builds_entered.set()
        await both_builds_entered.wait()
        return BuildResult(
            task_id=input_ref,
            created=False,
            initial_lifecycle=project_id,
            applied_stages_skipped=[],
            tick_dispatched=0,
        )

    monkeypatch.setattr(build_lifecycle, "_build_impl", build_impl)
    opts = BuildOptions(dry_run=True)

    results = await asyncio.gather(
        build_lifecycle._build_dry_run("#first", opts, db=database, project_id="first-project"),
        build_lifecycle._build_dry_run("#second", opts, db=database, project_id="second-project"),
    )

    assert entered_count == 2
    assert [result.dry_run for result in results] == [True, True]


@pytest.mark.asyncio
async def test_await_task_completion_propagates_inner_cancellation_without_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_pool_module()
    shield_calls = 0

    async def cancelled_operation() -> None:
        raise asyncio.CancelledError("inner operation cancelled")

    async def bounded_shield(task: asyncio.Task[object]) -> object:
        nonlocal shield_calls
        shield_calls += 1
        if shield_calls > 1:
            raise AssertionError("cancelled inner task was awaited repeatedly")
        return await task

    monkeypatch.setattr(module.asyncio, "shield", bounded_shield)
    task = asyncio.create_task(cancelled_operation())

    with pytest.raises(asyncio.CancelledError, match="inner operation cancelled"):
        await module._await_task_completion(task)

    assert shield_calls == 1


@pytest.mark.asyncio
async def test_advisory_lock_does_not_consume_single_pool_connection(
    monkeypatch: pytest.MonkeyPatch,
    postgres_database_url: str,
    postgres_schema: str,
) -> None:
    module = _postgres_module()
    scoped_url = postgres_database_url + f"?options=-csearch_path%3D{postgres_schema}"
    db = module.PostgresHubDatabase(
        scoped_url,
        pool_config=PostgresPoolConfig(
            min_size=1,
            max_size=1,
            acquire_timeout_seconds=0.1,
        ),
    )

    try:
        db.open()
        async with db.advisory_lock(module.AgentCapAdmission(project_id=None)):
            row = await asyncio.to_thread(db.fetchone, "SELECT 1 AS value")
    finally:
        db.close()

    assert row == {"value": 1}


def test_advisory_lock_connection_reuses_pool_session_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _postgres_module()
    pool_module = _postgres_pool_module()
    calls: dict[str, object] = {}
    connection = object()

    def fake_connect(conninfo: str, **kwargs: object) -> object:
        calls["conninfo"] = conninfo
        calls["kwargs"] = kwargs
        return connection

    monkeypatch.setattr(pool_module.psycopg, "connect", fake_connect)
    db = module.PostgresHubDatabase(
        "postgresql://gobby:secret@localhost/gobby?options=-cstatement_timeout%3D5000",
        pool_config=PostgresPoolConfig(min_size=1, max_size=1),
    )

    try:
        result = db._open_advisory_lock_connection()
    finally:
        db.close()

    assert result is connection
    conninfo = calls["conninfo"]
    assert isinstance(conninfo, str)
    assert pool_module.conninfo_to_dict(conninfo)["options"] == (
        "-cstatement_timeout=5000 -ctimezone=UTC"
    )
    assert calls["kwargs"] == {
        "application_name": db.application_name,
        "autocommit": True,
        "prepare_threshold": None,
        "row_factory": pool_module.dict_row,
    }


def test_postgres_hub_database_exposes_backend_neutral_surface() -> None:
    module = _postgres_module()

    assert module.PostgresHubDatabase.dialect == "postgres"

    transaction = inspect.signature(module.PostgresHubDatabase.transaction)
    assert list(transaction.parameters) == ["self"]

    transaction_immediate = inspect.signature(module.PostgresHubDatabase.transaction_immediate)
    assert list(transaction_immediate.parameters) == ["self", "lock"]
    assert transaction_immediate.parameters["lock"].default is inspect.Parameter.empty

    advisory_lock = inspect.signature(module.PostgresHubDatabase.advisory_lock)
    assert list(advisory_lock.parameters) == ["self", "lock"]
    assert advisory_lock.parameters["lock"].default is inspect.Parameter.empty

    after_commit = inspect.signature(module.PostgresHubDatabase.after_commit)
    assert list(after_commit.parameters) == ["self", "callback"]

    for method in (
        "execute",
        "executemany",
        "fetchone",
        "fetchall",
        "safe_update",
        "apply_migrations",
        "close",
        "advisory_lock",
        "after_commit",
    ):
        assert hasattr(module.PostgresHubDatabase, method), method


class _FakePostgresConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, list[tuple[object, ...]]]] = []

    def execute(self, sql: str, params: object = ()):
        self.execute_calls.append((sql, params))
        return object()

    def executemany(self, sql: str, rows) -> None:
        self.executemany_calls.append((sql, [tuple(row) for row in rows]))


def test_postgres_transaction_execute_passes_sql_and_params_directly() -> None:
    module = _postgres_pool_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.execute("SELECT %s, %s, 'literal%%'", ("one", "two"))

    assert conn.execute_calls == [("SELECT %s, %s, 'literal%%'", ("one", "two"))]


def test_postgres_transaction_execute_preserves_mapping_params() -> None:
    module = _postgres_pool_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)
    params = {"value": 1, "title": "demo"}

    tx.execute("SELECT %(value)s WHERE title = %(title)s", params)

    assert conn.execute_calls == [("SELECT %(value)s WHERE title = %(title)s", params)]


def test_postgres_transaction_executemany_passes_rows_directly() -> None:
    module = _postgres_pool_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.executemany(
        "INSERT INTO direct_rows (a, b) VALUES (%s, %s)",
        [("left", "right"), ("first", "second")],
    )

    assert conn.executemany_calls == [
        (
            "INSERT INTO direct_rows (a, b) VALUES (%s, %s)",
            [("left", "right"), ("first", "second")],
        )
    ]


def test_postgres_transaction_executemany_empty_rows_skips_driver() -> None:
    module = _postgres_pool_module()
    conn = _FakePostgresConnection()
    tx = module._PostgresTransaction(conn)

    tx.executemany("INSERT INTO direct_rows (a) VALUES (%s)", [])

    assert conn.executemany_calls == []


def test_postgres_safe_update_builds_psycopg_where_style() -> None:
    module = _postgres_module()

    assert module._build_safe_update(
        "sessions",
        {"message_count": 3, "turn_count": 2},
        "id = %s",
        ("session-1",),
    ) == (
        "UPDATE sessions SET message_count = %s, turn_count = %s WHERE id = %s",
        (3, 2, "session-1"),
    )


def test_postgres_safe_update_rejects_invalid_identifiers() -> None:
    module = _postgres_module()

    with pytest.raises(ValueError, match="invalid SQL identifier"):
        module._build_safe_update(
            "sessions; DROP TABLE sessions", {"status": "paused"}, "id = %s", ()
        )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def test_postgres_cursor_normalizes_jsonb_values_to_storage_json_text() -> None:
    module = _postgres_pool_module()
    cursor = module._PostgresCursor(
        _FakeResult(
            [
                {
                    "name": "profile",
                    "skip_stages_json": ["merge", "qa"],
                    "metadata_json": {"b": 2, "a": 1},
                }
            ]
        )
    )

    assert cursor.fetchone() == {
        "name": "profile",
        "skip_stages_json": '["merge","qa"]',
        "metadata_json": '{"a":1,"b":2}',
    }


def test_postgres_cursor_preserves_datetime_values_as_aware_utc() -> None:
    module = _postgres_pool_module()
    cursor = module._PostgresCursor(
        _FakeResult(
            [
                {
                    "id": "cron",
                    "last_run_at": datetime(
                        2026,
                        5,
                        21,
                        0,
                        30,
                        tzinfo=timezone(-timedelta(hours=5)),
                    ),
                }
            ]
        )
    )

    row = cursor.fetchone()

    assert row is not None
    assert row == {
        "id": "cron",
        "last_run_at": datetime(2026, 5, 21, 5, 30, tzinfo=UTC),
    }
    assert isinstance(row["last_run_at"], datetime)


def test_postgres_cursor_preserves_date_values_as_dates() -> None:
    module = _postgres_pool_module()
    due_date = date(2026, 5, 21)
    cursor = module._PostgresCursor(_FakeResult([{"id": "task", "due_date": due_date}]))

    row = cursor.fetchone()

    assert row is not None
    assert row == {"id": "task", "due_date": due_date}
    assert not isinstance(row["due_date"], datetime)


def test_postgres_conninfo_preserves_options_and_forces_utc_timezone() -> None:
    module = _postgres_pool_module()
    conninfo = module._conninfo_with_utc_session_timezone(
        "postgresql://user:pass@localhost:5432/gobby?options=-cstatement_timeout%3D5000"
    )

    parsed = module.conninfo_to_dict(conninfo)

    assert parsed["options"] == "-cstatement_timeout=5000 -ctimezone=UTC"
