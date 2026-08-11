from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, TypeVar, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.app import DaemonConfig
from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.runner import GobbyRunner
from gobby.runner_lifecycle_periodic import start_periodic_tasks
from gobby.runner_maintenance import bin_freshness_loop
from gobby.storage.hub.protocol import HubDatabase
from tests.config_runtime_helpers import static_runtime_capture

pytestmark = pytest.mark.unit

T = TypeVar("T")


def _capture_bin_config(config: BinFreshnessConfig) -> Callable[[], RuntimeActiveBundle]:
    return static_runtime_capture(DaemonConfig(bin_freshness=config))


@pytest.mark.asyncio
async def test_bin_freshness_loop_initial_delay_interval_jitter() -> None:
    sleeps: list[float] = []
    updates = 0
    shutdown = False

    async def fake_sleep(duration: float) -> None:
        nonlocal shutdown
        sleeps.append(duration)
        if len(sleeps) == 2:
            shutdown = True

    def update_once(db: object, config: BinFreshnessConfig) -> list[object]:
        nonlocal updates
        updates += 1
        return []

    await bin_freshness_loop(
        cast(HubDatabase, object()),
        lambda: shutdown,
        capture_bundle=_capture_bin_config(
            BinFreshnessConfig(
                initial_delay_seconds=3,
                interval_seconds=10,
                jitter_seconds=5,
            )
        ),
        update_once=update_once,
        sleep=fake_sleep,
        jitter=lambda _upper: 2,
    )

    assert sleeps == [3, 12]
    assert updates == 1


@pytest.mark.asyncio
async def test_bin_freshness_loop_routes_updates_through_run_db() -> None:
    sleeps: list[float] = []
    run_db_calls: list[object] = []
    shutdown = False

    async def fake_sleep(duration: float) -> None:
        nonlocal shutdown
        sleeps.append(duration)
        shutdown = True

    def update_once(db: object, config: BinFreshnessConfig) -> list[object]:
        assert config.enabled is True
        return [db]

    async def run_db(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        run_db_calls.append(func)
        return func(*args, **kwargs)

    await bin_freshness_loop(
        cast(HubDatabase, object()),
        lambda: shutdown,
        capture_bundle=_capture_bin_config(
            BinFreshnessConfig(initial_delay_seconds=0, interval_seconds=10, jitter_seconds=0)
        ),
        update_once=update_once,
        run_db=run_db,
        sleep=fake_sleep,
    )

    assert sleeps == [10]
    assert run_db_calls == [update_once]


@pytest.mark.asyncio
async def test_bin_freshness_loop_default_jitter_uses_system_random_source() -> None:
    sleeps: list[float] = []
    updates = 0
    shutdown = False

    async def fake_sleep(duration: float) -> None:
        nonlocal shutdown
        sleeps.append(duration)
        shutdown = True

    def update_once(db: object, config: BinFreshnessConfig) -> list[object]:
        nonlocal updates
        updates += 1
        return []

    with patch(
        "gobby.runner_maintenance.binaries._JITTER_RANDOM",
        SimpleNamespace(uniform=lambda _lower, _upper: 2),
    ):
        await bin_freshness_loop(
            object(),
            lambda: shutdown,
            capture_bundle=_capture_bin_config(
                BinFreshnessConfig(
                    initial_delay_seconds=0,
                    interval_seconds=10,
                    jitter_seconds=5,
                )
            ),
            update_once=update_once,
            sleep=fake_sleep,
        )

    assert sleeps == [12]
    assert updates == 1


@pytest.mark.asyncio
async def test_bin_freshness_loop_shutdown_exit_before_initial_delay() -> None:
    """Shutdown is checked before initial sleep so no freshness update is scheduled."""
    updates = 0

    def update_once(db: object, config: BinFreshnessConfig) -> list[object]:
        nonlocal updates
        updates += 1
        return []

    await bin_freshness_loop(
        object(),
        lambda: True,
        capture_bundle=_capture_bin_config(BinFreshnessConfig()),
        update_once=update_once,
    )

    assert updates == 0


@pytest.mark.asyncio
async def test_bin_freshness_loop_disabled_skips_updates() -> None:
    updates = 0
    shutdown = False

    def update_once(db: object, config: BinFreshnessConfig) -> list[object]:
        nonlocal updates
        updates += 1
        return []

    async def fake_sleep(_duration: float) -> None:
        nonlocal shutdown
        shutdown = True

    await bin_freshness_loop(
        object(),
        lambda: shutdown,
        capture_bundle=_capture_bin_config(BinFreshnessConfig(enabled=False)),
        update_once=update_once,
        sleep=fake_sleep,
    )

    assert updates == 0


def test_disabled_config_keeps_periodic_task_registered_for_live_enable() -> None:
    capture_bundle = static_runtime_capture(
        DaemonConfig(bin_freshness=BinFreshnessConfig(enabled=False))
    )

    runner = SimpleNamespace(
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        memory_manager=None,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        _shutdown_requested=False,
        config_runtime=SimpleNamespace(capture=capture_bundle),
    )

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    def fake_create_task(coro: object, *, name: str | None = None) -> MagicMock:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        task = MagicMock()
        task.name = name
        return task

    with patch("gobby.runner_lifecycle_periodic.asyncio.create_task", side_effect=fake_create_task):
        start_periodic_tasks(
            cast(GobbyRunner, runner),
            tracker=None,
            metrics_cleanup_loop=noop,
            metrics_archive_loop=noop,
            span_cleanup_loop=noop,
            cleanup_zombie_messages_loop=noop,
            cleanup_comms_messages_loop=noop,
            cleanup_chat_attachments_loop=noop,
            cleanup_expired_isolation_loop=noop,
            metric_snapshot_loop=noop,
            drain_hook_inbox_loop=noop,
            expire_approval_timeouts_loop=noop,
        )

    assert runner._bin_freshness_task.name == "bin-freshness"
    assert runner._metrics_cleanup_task.name == "metrics-cleanup"
    assert runner._tool_results_cleanup_task.name == "tool-result-cleanup"


def test_chat_attachment_periodic_uses_runtime_capture() -> None:
    capture_bundle = static_runtime_capture(
        DaemonConfig(bin_freshness=BinFreshnessConfig(enabled=False))
    )

    runner = SimpleNamespace(
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        memory_manager=None,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        _shutdown_requested=False,
        config_runtime=SimpleNamespace(capture=capture_bundle),
    )
    cleanup_kwargs: dict[str, object] = {}

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    def cleanup_chat_attachments_loop(*args: object, **kwargs: object) -> object:
        cleanup_kwargs.update(kwargs)
        return noop()

    def fake_create_task(coro: object, *, name: str | None = None) -> MagicMock:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        task = MagicMock()
        task.name = name
        return task

    with patch("gobby.runner_lifecycle_periodic.asyncio.create_task", side_effect=fake_create_task):
        start_periodic_tasks(
            cast(GobbyRunner, runner),
            tracker=None,
            metrics_cleanup_loop=noop,
            metrics_archive_loop=noop,
            span_cleanup_loop=noop,
            cleanup_zombie_messages_loop=noop,
            cleanup_comms_messages_loop=noop,
            cleanup_chat_attachments_loop=cleanup_chat_attachments_loop,
            cleanup_expired_isolation_loop=noop,
            metric_snapshot_loop=noop,
            drain_hook_inbox_loop=noop,
            expire_approval_timeouts_loop=noop,
        )

    assert cleanup_kwargs["capture_bundle"] is capture_bundle
