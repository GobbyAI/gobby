from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

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

if TYPE_CHECKING:
    from gobby.runner import GobbyRunner

_DAY_SECONDS = 24 * 60 * 60
_TEST_STARTUP_DELAY_SECONDS = 10.0


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


def test_schema_sweep_delegates_to_gdaemon(monkeypatch: pytest.MonkeyPatch) -> None:
    sweep = MagicMock()
    monkeypatch.setattr(storage_hygiene, "sweep_test_schemas", sweep, raising=False)

    result = storage_hygiene.sweep_orphaned_test_schemas("postgresql://test", age_hours=12)

    assert result is None
    sweep.assert_called_once_with("postgresql://test", age_hours=12)


@pytest.mark.asyncio
async def test_schema_sweep_loop_rechecks_after_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep = CancelAtInterval()
    sweep = MagicMock(return_value=0)
    monkeypatch.setattr(storage_hygiene, "sweep_orphaned_test_schemas", sweep)

    await storage_hygiene.sweep_test_schemas_loop(
        "postgresql://test",
        lambda: False,
        interval_seconds=60,
        sleep=sleep,
    )

    assert sweep.call_args_list == [call("postgresql://test"), call("postgresql://test")]
    assert sleep.requests == [60, 60]


@pytest.mark.asyncio
async def test_periodic_start_schedules_test_schema_sweep_loop() -> None:
    database_url = "postgresql://gobby:test@localhost:5432/gobby"
    calls: list[tuple[str | None, Callable[[], bool]]] = []

    async def complete_loop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def capture_sweep(url: str | None, is_shutdown: Callable[[], bool]) -> None:
        calls.append((url, is_shutdown))

    loops = dict.fromkeys(_default_loops(), complete_loop)
    loops["sweep_test_schemas_loop"] = capture_sweep
    runner = cast(
        "GobbyRunner",
        SimpleNamespace(
            config_runtime=SimpleNamespace(
                capture=lambda: SimpleNamespace(
                    snapshot=SimpleNamespace(active=DaemonConfig(database_url=database_url))
                )
            ),
            metrics_manager=object(),
            metrics_event_store=object(),
            database=object(),
            db_executor=None,
            memory_manager=None,
            http_server=SimpleNamespace(app=object()),
            pipeline_execution_manager=None,
            session_manager=None,
            _shutdown_requested=False,
        ),
    )

    start_periodic_tasks(runner, tracker=None, **loops)
    await asyncio.gather(
        *(task for task in vars(runner).values() if isinstance(task, asyncio.Task))
    )

    assert len(calls) == 1
    assert calls[0][0] == database_url
    assert calls[0][1]() is False


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

    def delete_messages_before(*_args: Any, **_kwargs: Any) -> tuple[int, list[Any]]:
        work_times.append(sleep.elapsed)
        return 0, []

    store.delete_messages_before.side_effect = delete_messages_before
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
