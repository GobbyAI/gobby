from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.runner_lifecycle_periodic import _default_loops, start_periodic_tasks
from gobby.runner_maintenance import cleanup_comms_messages_loop, storage_hygiene
from gobby.runner_maintenance_recurring import (
    _MAINTENANCE_STARTUP_WINDOW_SECONDS,
    _deterministic_startup_delay,
    memory_reconcile_loop,
    metrics_archive_loop,
    metrics_cleanup_loop,
)

_DAY_SECONDS = 24 * 60 * 60
_TEST_STARTUP_DELAY_SECONDS = 10.0


def _schema_sweep_connection(
    schema_name: str,
    *,
    lease_acquired: bool,
) -> tuple[MagicMock, list[str]]:
    events: list[str] = []
    connection = MagicMock()
    connection.__enter__.return_value = connection

    def execute(query: object, params: object | None = None) -> MagicMock:
        del params
        rendered = str(query)
        result = MagicMock()
        if "schema_name LIKE" in rendered:
            events.append("scan")
            result.fetchall.return_value = [(schema_name,)]
        elif "pg_try_advisory_lock" in rendered:
            events.append("try-lease")
            result.fetchone.return_value = (lease_acquired,)
        elif "schema_name =" in rendered:
            events.append("recheck")
            result.fetchone.return_value = (schema_name,)
        elif "DROP SCHEMA" in rendered:
            events.append("drop")
        elif "pg_advisory_unlock" in rendered:
            events.append("unlock")
        return result

    connection.execute.side_effect = execute
    return connection, events


class CancelAtInterval:
    def __init__(self) -> None:
        self.elapsed = 0.0
        self.requests: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.requests.append(seconds)
        if len(self.requests) == 2:
            raise asyncio.CancelledError
        self.elapsed += seconds


def test_default_startup_offsets_are_stable_bounded_and_staggered() -> None:
    names = (
        "metrics-cleanup",
        "metrics-archive",
        "memory-reconcile",
        "comms-message-cleanup",
    )
    first = [
        _deterministic_startup_delay(name, window_seconds=_MAINTENANCE_STARTUP_WINDOW_SECONDS)
        for name in names
    ]
    second = [
        _deterministic_startup_delay(name, window_seconds=_MAINTENANCE_STARTUP_WINDOW_SECONDS)
        for name in names
    ]

    assert first == second
    assert len(set(first)) == len(names)
    assert all(1 <= delay <= _MAINTENANCE_STARTUP_WINDOW_SECONDS for delay in first)


@pytest.mark.asyncio
async def test_metrics_cleanup_runs_before_24_hours_then_waits_for_normal_interval() -> None:
    sleep = CancelAtInterval()
    work_times: list[float] = []
    manager = MagicMock()
    manager.cleanup_old_metrics.side_effect = lambda: work_times.append(sleep.elapsed) or 0

    await metrics_cleanup_loop(
        manager,
        lambda: False,
        startup_delay_seconds=_TEST_STARTUP_DELAY_SECONDS,
        sleep=sleep,
    )

    assert work_times == [_TEST_STARTUP_DELAY_SECONDS]
    assert sleep.requests == [_TEST_STARTUP_DELAY_SECONDS, _DAY_SECONDS]


@pytest.mark.parametrize(
    "schema_name",
    [
        "gobby_test_malformed",
        "gobby_test_100_1_master_abc123",
    ],
)
def test_schema_sweep_rechecks_eligibility_under_acquired_lease(
    monkeypatch: pytest.MonkeyPatch,
    schema_name: str,
) -> None:
    connection, events = _schema_sweep_connection(schema_name, lease_acquired=True)
    monkeypatch.setattr(storage_hygiene.psycopg, "connect", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(storage_hygiene.time, "time", lambda: 100)

    dropped = storage_hygiene.sweep_orphaned_test_schemas("postgresql://test")

    assert dropped == 0
    assert events == ["scan", "try-lease", "recheck", "unlock"]


def test_schema_sweep_skips_schema_with_held_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, events = _schema_sweep_connection(
        "gobby_test_0_1_master_abc123",
        lease_acquired=False,
    )
    monkeypatch.setattr(storage_hygiene.psycopg, "connect", lambda *_args, **_kwargs: connection)

    dropped = storage_hygiene.sweep_orphaned_test_schemas("postgresql://test")

    assert dropped == 0
    assert events == ["scan", "try-lease"]


def test_schema_sweep_drops_eligible_schema_only_after_recheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_name = "gobby_test_0_1_master_abc123"
    connection, events = _schema_sweep_connection(schema_name, lease_acquired=True)
    monkeypatch.setattr(storage_hygiene.psycopg, "connect", lambda *_args, **_kwargs: connection)

    dropped = storage_hygiene.sweep_orphaned_test_schemas("postgresql://test")

    assert dropped == 1
    assert events == ["scan", "try-lease", "recheck", "drop", "unlock"]


@pytest.mark.asyncio
async def test_periodic_start_schedules_test_schema_startup_sweep() -> None:
    database_url = "postgresql://gobby:test@localhost:5432/gobby"
    calls: list[str | None] = []

    async def complete_loop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def capture_sweep(url: str | None) -> None:
        calls.append(url)

    loops = dict.fromkeys(_default_loops(), complete_loop)
    loops["sweep_test_schemas_on_startup"] = capture_sweep
    runner = SimpleNamespace(
        config=DaemonConfig(database_url=database_url),
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        db_executor=None,
        memory_manager=None,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        session_manager=None,
        _shutdown_requested=False,
    )

    start_periodic_tasks(runner, tracker=None, **loops)
    await asyncio.gather(
        *(task for task in vars(runner).values() if isinstance(task, asyncio.Task))
    )

    assert calls == [database_url]


@pytest.mark.asyncio
async def test_metrics_archive_runs_before_24_hours_then_waits_for_normal_interval() -> None:
    sleep = CancelAtInterval()
    work_times: list[float] = []
    event_store = MagicMock()
    event_store.archive_old_events.side_effect = (
        lambda **_kwargs: work_times.append(sleep.elapsed) or 0
    )

    await metrics_archive_loop(
        event_store,
        lambda: False,
        startup_delay_seconds=_TEST_STARTUP_DELAY_SECONDS,
        sleep=sleep,
    )

    assert work_times == [_TEST_STARTUP_DELAY_SECONDS]
    assert sleep.requests == [_TEST_STARTUP_DELAY_SECONDS, _DAY_SECONDS]


@pytest.mark.asyncio
async def test_memory_reconcile_runs_before_24_hours_then_waits_for_normal_interval() -> None:
    sleep = CancelAtInterval()
    work_times: list[float] = []
    memory_manager = MagicMock()

    async def reconcile_stores(*, dry_run: bool) -> dict[str, object]:
        assert dry_run is False
        work_times.append(sleep.elapsed)
        return {}

    memory_manager.reconcile_stores = AsyncMock(side_effect=reconcile_stores)

    await memory_reconcile_loop(
        memory_manager,
        lambda: False,
        startup_delay_seconds=_TEST_STARTUP_DELAY_SECONDS,
        sleep=sleep,
    )

    assert work_times == [_TEST_STARTUP_DELAY_SECONDS]
    assert sleep.requests == [_TEST_STARTUP_DELAY_SECONDS, _DAY_SECONDS]


@pytest.mark.asyncio
async def test_comms_cleanup_runs_before_24_hours_then_waits_for_normal_interval() -> None:
    sleep = CancelAtInterval()
    work_times: list[float] = []
    store = MagicMock()
    store.delete_messages_before.side_effect = lambda *_args, **_kwargs: (
        work_times.append(sleep.elapsed) or 0,
        [],
    )
    attachment_manager = MagicMock()
    attachment_manager.delete_paths.return_value = 0
    attachment_manager.cleanup_old.return_value = 0

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    async def run_in_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    with (
        patch("gobby.storage.communications.LocalCommunicationsStore", return_value=store),
        patch(
            "gobby.communications.attachments.AttachmentManager", return_value=attachment_manager
        ),
        patch("asyncio.to_thread", new=AsyncMock(side_effect=run_in_thread)),
    ):
        await cleanup_comms_messages_loop(
            MagicMock(),
            lambda: False,
            run_db=run_db,
            startup_delay_seconds=_TEST_STARTUP_DELAY_SECONDS,
            sleep=sleep,
        )

    assert work_times == [_TEST_STARTUP_DELAY_SECONDS]
    assert sleep.requests == [_TEST_STARTUP_DELAY_SECONDS, _DAY_SECONDS]
