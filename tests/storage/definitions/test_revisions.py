"""Domain cache revisions and the definition-revision listener."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import psycopg
import pytest
from psycopg import sql

from gobby.runner import GobbyRunner
from gobby.runner_init import services
from gobby.runner_lifecycle_shutdown import _run_async_shutdown_cleanup
from gobby.runner_rollback import rollback_runner_resources
from gobby.shutdown_intent import ShutdownIntent
from gobby.storage.definitions.notifications import DefinitionRevisionListener
from gobby.storage.definitions.revisions import (
    DEFINITION_DOMAINS,
    DefinitionDomain,
    RevisionExecutor,
    advance_persistent_revision,
    bump_definitions_revision,
    fetch_persistent_revisions,
    get_definitions_revision,
    register_revision_listener,
    reset_definition_revision_state,
)
from gobby.storage.hub.postgres import PostgresHubDatabase
from tests.storage.definitions.conftest import scoped_postgres_dsn

_CREATE_REVISIONS_TABLE = """
CREATE TABLE definition_revisions (
    domain text PRIMARY KEY,
    revision bigint DEFAULT 0 NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
)
"""


@pytest.fixture(autouse=True)
def _reset_revision_globals() -> Iterator[None]:
    reset_definition_revision_state()
    yield
    reset_definition_revision_state()


class FakeNotification:
    def __init__(self, payload: str) -> None:
        self.payload = payload


class FakeRevisionConnection:
    def __init__(self, events: asyncio.Queue[FakeNotification | BaseException]) -> None:
        self.autocommit = True
        self.events = events
        self.closed = False
        self.commands: list[str] = []

    async def execute(self, command: str) -> object:
        self.commands.append(command)
        return object()

    def notifies(
        self,
        *,
        timeout: float | None = None,
        stop_after: int | None = None,
    ) -> AsyncIterator[FakeNotification]:
        del timeout, stop_after

        async def iterator() -> AsyncIterator[FakeNotification]:
            while not self.closed:
                item = await self.events.get()
                if isinstance(item, BaseException):
                    raise item
                yield item

        return iterator()

    async def close(self) -> None:
        self.closed = True


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _notify_from_a_foreign_backend(conninfo: str, payload: str) -> None:
    """Notify the revision channel from a backend the caller does not own.

    LISTEN/NOTIFY is scoped to the database, never to a schema, so a per-test
    schema cannot keep other writers off `gobby_definition_revisions`. Any
    concurrent pytest process, xdist worker, or daemon sharing the test
    database publishes onto the same channel. Emitting one deliberately turns
    that race into a fixed precondition.
    """
    with psycopg.connect(conninfo, autocommit=True) as foreign:
        foreign.execute("SELECT pg_notify(%s, %s)", ("gobby_definition_revisions", payload))


def _backend_pid(conn: RevisionExecutor) -> int:
    """Return the server pid of the backend this connection is bound to."""
    row = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
    assert row is not None
    return int(row["pid"])


def _notifications_from(
    listener: psycopg.Connection[Any],
    backend_pid: int,
    *,
    timeout: float,
) -> list[str]:
    """Collect payloads `backend_pid` published, discarding every foreign one.

    Returns as soon as one of our own notifications arrives, otherwise after
    `timeout` seconds. `Notify.pid` is the notifying backend's server pid, so
    it is what separates our own publication from everyone else's on a
    database-wide channel.
    """
    payloads: list[str] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return payloads
        for notification in listener.notifies(timeout=remaining, stop_after=1):
            if notification.pid == backend_pid:
                payloads.append(notification.payload)
                return payloads


@pytest.fixture
def revision_schema_url(postgres_database_url: str) -> Iterator[str]:
    schema = f"gobby_test_defrev_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.execute(_CREATE_REVISIONS_TABLE)
    scoped = scoped_postgres_dsn(postgres_database_url, schema)
    try:
        yield scoped
    finally:
        with psycopg.connect(postgres_database_url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def revision_db(revision_schema_url: str) -> Iterator[PostgresHubDatabase]:
    database = PostgresHubDatabase(revision_schema_url)
    try:
        yield database
    finally:
        database.close()


def test_bump_one_domain_fires_only_that_domain_listeners() -> None:
    fired: list[str] = []
    register_revision_listener("rules", lambda: fired.append("rules"))
    register_revision_listener("pipelines", lambda: fired.append("pipelines"))

    before_rules = get_definitions_revision("rules")
    before_agents = get_definitions_revision("agents")
    before_pipelines = get_definitions_revision("pipelines")

    bump_definitions_revision("rules")

    assert get_definitions_revision("rules") == before_rules + 1
    assert get_definitions_revision("agents") == before_agents
    assert get_definitions_revision("pipelines") == before_pipelines
    assert fired == ["rules"]


def test_bump_is_thread_safe() -> None:
    workers = 8
    per_worker = 25

    def bump_many() -> None:
        for _ in range(per_worker):
            bump_definitions_revision("agents")

    threads = [threading.Thread(target=bump_many) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert get_definitions_revision("agents") == workers * per_worker
    assert get_definitions_revision("rules") == 0


def test_listener_exception_does_not_propagate_to_bump_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom() -> None:
        raise RuntimeError("listener boom")

    register_revision_listener("variables", boom)
    with caplog.at_level(logging.ERROR):
        bump_definitions_revision("variables")
    assert get_definitions_revision("variables") == 1
    assert "listener boom" in caplog.text


def test_advance_persistent_revision_commits_and_notifies(
    revision_db: PostgresHubDatabase,
) -> None:
    with psycopg.connect(revision_db.conninfo, autocommit=True) as listener:
        listener.execute("LISTEN gobby_definition_revisions")
        _notify_from_a_foreign_backend(revision_db.conninfo, "rules:4242")
        with revision_db.transaction() as txn:
            notifier_pid = _backend_pid(txn)
            advance_persistent_revision(txn, "rules")
        notifications = _notifications_from(listener, notifier_pid, timeout=2.0)

    row = revision_db.fetchone(
        "SELECT revision FROM definition_revisions WHERE domain = %s",
        ("rules",),
    )
    assert row is not None
    assert int(row["revision"]) == 1
    assert notifications == ["rules:1"]


def test_advance_persistent_revision_rollback_is_silent(
    revision_db: PostgresHubDatabase,
) -> None:
    with psycopg.connect(revision_db.conninfo, autocommit=True) as listener:
        listener.execute("LISTEN gobby_definition_revisions")
        _notify_from_a_foreign_backend(revision_db.conninfo, "rules:4242")
        notifier_pid = -1
        with pytest.raises(RuntimeError, match="rollback"):
            with revision_db.transaction() as txn:
                notifier_pid = _backend_pid(txn)
                advance_persistent_revision(txn, "rules")
                raise RuntimeError("rollback")
        assert notifier_pid > 0
        notifications = _notifications_from(listener, notifier_pid, timeout=0.2)

    row = revision_db.fetchone(
        "SELECT revision FROM definition_revisions WHERE domain = %s",
        ("rules",),
    )
    assert row is None
    assert notifications == []


def test_ambient_nested_commit_visibility(revision_db: PostgresHubDatabase) -> None:
    before = get_definitions_revision("rules")
    fired: list[str] = []
    register_revision_listener("rules", lambda: fired.append("rules"))

    with revision_db.transaction() as outer:
        with revision_db.transaction() as inner:
            assert inner is outer
            advance_persistent_revision(inner, "rules")
            inner.after_commit(lambda: bump_definitions_revision("rules"))
        assert get_definitions_revision("rules") == before
        assert fired == []

    assert get_definitions_revision("rules") == before + 1
    assert fired == ["rules"]
    row = revision_db.fetchone(
        "SELECT revision FROM definition_revisions WHERE domain = %s",
        ("rules",),
    )
    assert row is not None
    assert int(row["revision"]) == 1


def test_ambient_nested_rollback_is_silent(revision_db: PostgresHubDatabase) -> None:
    before = get_definitions_revision("pipelines")
    fired: list[str] = []
    register_revision_listener("pipelines", lambda: fired.append("pipelines"))

    with pytest.raises(RuntimeError, match="outer rollback"):
        with revision_db.transaction():
            with revision_db.transaction() as inner:
                advance_persistent_revision(inner, "pipelines")
                inner.after_commit(lambda: bump_definitions_revision("pipelines"))
            raise RuntimeError("outer rollback")

    assert get_definitions_revision("pipelines") == before
    assert fired == []
    row = revision_db.fetchone(
        "SELECT revision FROM definition_revisions WHERE domain = %s",
        ("pipelines",),
    )
    assert row is None


@pytest.mark.asyncio
async def test_listener_maps_notify_into_local_bump() -> None:
    events: asyncio.Queue[FakeNotification | BaseException] = asyncio.Queue()
    connections: list[FakeRevisionConnection] = []

    async def factory() -> FakeRevisionConnection:
        connection = FakeRevisionConnection(events)
        connections.append(connection)
        return connection

    observed = dict.fromkeys(DEFINITION_DOMAINS, 0)
    listener = DefinitionRevisionListener(
        factory,
        fetch_revisions=lambda: observed,
        poll_interval=30.0,
    )
    fired: list[str] = []
    register_revision_listener("rules", lambda: fired.append("rules"))
    register_revision_listener("agents", lambda: fired.append("agents"))

    await listener.start()
    try:
        await events.put(FakeNotification("rules:4"))
        await wait_until(lambda: get_definitions_revision("rules") == 1)
        assert fired == ["rules"]
        assert get_definitions_revision("agents") == 0
        assert connections[0].commands == ["LISTEN gobby_definition_revisions"]
    finally:
        await listener.close()
        assert connections[0].closed is True


@pytest.mark.asyncio
async def test_poll_healing_recovers_missed_notification() -> None:
    events: asyncio.Queue[FakeNotification | BaseException] = asyncio.Queue()
    persistent = dict.fromkeys(DEFINITION_DOMAINS, 0)

    async def factory() -> FakeRevisionConnection:
        return FakeRevisionConnection(events)

    listener = DefinitionRevisionListener(
        factory,
        fetch_revisions=lambda: dict(persistent),
        poll_interval=0.01,
    )
    await listener.start()
    try:
        assert get_definitions_revision("variables") == 0
        persistent["variables"] = 6
        await wait_until(lambda: get_definitions_revision("variables") == 1)
    finally:
        await listener.close()


@pytest.mark.asyncio
async def test_start_seeds_without_spurious_listener_fires() -> None:
    events: asyncio.Queue[FakeNotification | BaseException] = asyncio.Queue()
    persistent = dict.fromkeys(DEFINITION_DOMAINS, 0)
    persistent["rules"] = 9
    fired: list[str] = []
    register_revision_listener("rules", lambda: fired.append("rules"))

    async def factory() -> FakeRevisionConnection:
        return FakeRevisionConnection(events)

    fetch_calls = {"count": 0}

    def fetch() -> dict[DefinitionDomain, int]:
        fetch_calls["count"] += 1
        return dict(persistent)

    listener = DefinitionRevisionListener(
        factory,
        fetch_revisions=fetch,
        poll_interval=0.02,
    )
    await listener.start()
    try:
        await wait_until(lambda: fetch_calls["count"] >= 2)
        assert fired == []
        assert get_definitions_revision("rules") == 0
    finally:
        await listener.close()


@pytest.mark.asyncio
async def test_listen_crash_reconnects_and_poll_covers_gap() -> None:
    events: asyncio.Queue[FakeNotification | BaseException] = asyncio.Queue()
    connections: list[FakeRevisionConnection] = []
    persistent = dict.fromkeys(DEFINITION_DOMAINS, 0)

    async def factory() -> FakeRevisionConnection:
        connection = FakeRevisionConnection(events)
        connections.append(connection)
        return connection

    listener = DefinitionRevisionListener(
        factory,
        fetch_revisions=lambda: dict(persistent),
        poll_interval=0.01,
        reconnect_backoff=0.01,
    )
    await listener.start()
    try:
        await events.put(ConnectionError("listen socket died"))
        await wait_until(lambda: len(connections) >= 2)
        persistent["agents"] = 2
        await wait_until(lambda: get_definitions_revision("agents") == 1)
        assert len(connections) >= 2
        assert get_definitions_revision("agents") == 1
        assert get_definitions_revision("rules") == 0
    finally:
        await listener.close()


@pytest.mark.asyncio
async def test_close_cancels_tasks_and_closes_connection() -> None:
    events: asyncio.Queue[FakeNotification | BaseException] = asyncio.Queue()
    connections: list[FakeRevisionConnection] = []

    async def factory() -> FakeRevisionConnection:
        connection = FakeRevisionConnection(events)
        connections.append(connection)
        return connection

    listener = DefinitionRevisionListener(
        factory,
        fetch_revisions=lambda: dict.fromkeys(DEFINITION_DOMAINS, 0),
        poll_interval=30.0,
    )
    await listener.start()
    listen_task = listener.listen_task
    poll_task = listener.poll_task
    assert listen_task is not None
    assert poll_task is not None
    await listener.close()
    assert listen_task.cancelled() or listen_task.done()
    assert poll_task.cancelled() or poll_task.done()
    assert listener.listen_task is None
    assert listener.poll_task is None
    assert connections[0].closed is True


@pytest.mark.asyncio
async def test_seed_and_poll_fetch_revisions_off_event_loop() -> None:
    events: asyncio.Queue[FakeNotification | BaseException] = asyncio.Queue()
    persistent = dict.fromkeys(DEFINITION_DOMAINS, 0)
    fetch_loops: list[asyncio.AbstractEventLoop | None] = []
    main_loop = asyncio.get_running_loop()

    async def factory() -> FakeRevisionConnection:
        return FakeRevisionConnection(events)

    def fetch() -> dict[DefinitionDomain, int]:
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        fetch_loops.append(running)
        return dict(persistent)

    listener = DefinitionRevisionListener(
        factory,
        fetch_revisions=fetch,
        poll_interval=0.01,
    )
    await listener.start()
    try:
        await wait_until(lambda: len(fetch_loops) >= 2)
        assert fetch_loops
        assert all(loop is not main_loop for loop in fetch_loops)
    finally:
        await listener.close()


@pytest.mark.asyncio
async def test_reconnect_backoff_increases_and_heals_from_persistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: asyncio.Queue[FakeNotification | BaseException] = asyncio.Queue()
    persistent = dict.fromkeys(DEFINITION_DOMAINS, 0)
    factory_calls = {"n": 0}
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def factory() -> FakeRevisionConnection:
        factory_calls["n"] += 1
        if factory_calls["n"] == 1:
            return FakeRevisionConnection(events)
        if factory_calls["n"] < 4:
            raise ConnectionError("connect failed")
        return FakeRevisionConnection(events)

    async def tracking_sleep(delay: float) -> None:
        if delay >= 10.0:
            await real_sleep(3600)
            return
        if delay >= 0.1:
            sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(
        "gobby.storage.definitions.notifications.asyncio.sleep",
        tracking_sleep,
    )

    listener = DefinitionRevisionListener(
        factory,
        fetch_revisions=lambda: dict(persistent),
        poll_interval=30.0,
        reconnect_backoff=0.1,
    )
    await listener.start()
    try:
        persistent["agents"] = 3
        await events.put(ConnectionError("listen socket died"))
        await wait_until(lambda: get_definitions_revision("agents") == 1)
        assert factory_calls["n"] >= 4
        assert sleeps[:3] == [0.1, 0.2, 0.4]
        assert sleeps[1] > sleeps[0]
        assert get_definitions_revision("agents") == 1
        assert get_definitions_revision("rules") == 0
    finally:
        await listener.close()


@pytest.mark.asyncio
async def test_init_stateful_services_starts_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = {"count": 0}

    async def start() -> None:
        started["count"] += 1

    runner = SimpleNamespace(definition_revision_listener=SimpleNamespace(start=start))

    async def noop_register(_runner: object) -> None:
        return None

    monkeypatch.setattr(services, "_init_stateful_dependencies", lambda _runner: None)
    monkeypatch.setattr(services, "_register_stateful_services", noop_register)
    monkeypatch.setattr(services, "_apply_stateful_services", lambda _runner: None)
    monkeypatch.setattr(services, "_init_project_context", lambda _runner: None)
    monkeypatch.setattr(services, "_schedule_scoped_tool_backfill", lambda _runner: None)

    await services.init_stateful_services(cast(Any, runner))
    assert started["count"] == 1
    assert runner.definition_revision_listener.start is start


@pytest.mark.asyncio
async def test_shutdown_closes_definition_revision_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gobby.runner_lifecycle_processes as runner_lifecycle_processes

    closed = {"count": 0}

    class Listener:
        async def close(self) -> None:
            closed["count"] += 1

    runner = SimpleNamespace(
        config_runtime=None,
        definition_revision_listener=Listener(),
    )
    monkeypatch.setattr(
        "gobby.runner_lifecycle_shutdown._settle_terminal_delivery_barrier",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "gobby.runner_lifecycle_shutdown._shutdown_database_concurrency",
        AsyncMock(),
    )
    monkeypatch.setattr(
        runner_lifecycle_processes,
        "_preserved_agent_terminal_pids",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        "gobby.telemetry.rule_allow_audit.shutdown_rule_allow_audit",
        AsyncMock(),
    )

    await _run_async_shutdown_cleanup(
        cast(GobbyRunner, runner),
        shutdown_intent=ShutdownIntent.STOP,
        reap_remaining_child_processes=AsyncMock(),
        shutdown_telemetry=MagicMock(),
    )
    assert closed["count"] == 1
    assert runner.definition_revision_listener is not None


def test_rollback_closes_unstarted_listener() -> None:
    closed = {"count": 0}

    class Listener:
        async def close(self) -> None:
            closed["count"] += 1

    runner = SimpleNamespace(definition_revision_listener=Listener())
    rollback_runner_resources(runner)
    assert closed["count"] == 1


def test_fetch_persistent_revisions_reads_table(revision_db: PostgresHubDatabase) -> None:
    with revision_db.transaction() as txn:
        advance_persistent_revision(txn, "rules", "pipelines")
    observed = fetch_persistent_revisions(revision_db)
    assert observed["rules"] == 1
    assert observed["pipelines"] == 1
    assert observed["agents"] == 0
    assert set(observed) == set(DEFINITION_DOMAINS)
