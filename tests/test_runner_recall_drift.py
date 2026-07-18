"""Wiring tests for the #17201 recall-drift monitor periodic task."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.config.bin_freshness import BinFreshnessConfig
from gobby.config.persistence import MemoryConfig
from gobby.runner_lifecycle_periodic import start_periodic_tasks

pytestmark = pytest.mark.unit


def _runner(*, memory_manager: object | None, memory_config: MemoryConfig | None) -> Any:
    config = SimpleNamespace(
        telemetry=SimpleNamespace(trace_retention_days=7),
        bin_freshness=BinFreshnessConfig(enabled=False),
        logging=object(),
        chat=None,
    )
    if memory_config is not None:
        config.memory = memory_config
    return SimpleNamespace(
        metrics_manager=object(),
        metrics_event_store=object(),
        database=object(),
        memory_manager=memory_manager,
        http_server=SimpleNamespace(app=object()),
        pipeline_execution_manager=None,
        degraded_services=set(),
        _shutdown_requested=False,
        config=config,
    )


def _start(runner: Any, **loop_overrides: Any) -> None:
    async def noop(*args: object, **kwargs: object) -> None:
        return None

    def fake_create_task(coro: object, *, name: str | None = None) -> MagicMock:
        close = getattr(coro, "close", None)
        if close is not None:
            close()
        task = MagicMock()
        task.name = name
        return task

    overrides = dict.fromkeys(
        (
            "metrics_cleanup_loop",
            "metrics_archive_loop",
            "span_cleanup_loop",
            "cleanup_zombie_messages_loop",
            "cleanup_comms_messages_loop",
            "cleanup_chat_attachments_loop",
            "cleanup_expired_isolation_loop",
            "metric_snapshot_loop",
            "drain_hook_inbox_loop",
            "expire_approval_timeouts_loop",
            "memory_reconcile_loop",
            "recall_drift_monitor_loop",
            "resource_monitor_loop",
        ),
        noop,
    )
    overrides.update(loop_overrides)
    with patch("gobby.runner_lifecycle_periodic.asyncio.create_task", side_effect=fake_create_task):
        start_periodic_tasks(runner, tracker=None, **overrides)


def test_enabled_monitor_starts_with_database_and_memory_config() -> None:
    memory_config = MemoryConfig(recall_drift_monitor_enabled=True)
    runner = _runner(memory_manager=object(), memory_config=memory_config)
    drift_args: list[tuple[Any, ...]] = []

    async def noop() -> None:
        return None

    def recall_drift_monitor_loop(*args: Any, **kwargs: Any) -> Any:
        drift_args.append(args)
        return noop()

    _start(runner, recall_drift_monitor_loop=recall_drift_monitor_loop)

    assert runner._recall_drift_task is not None
    db_arg, config_arg = drift_args[0][0], drift_args[0][1]
    assert db_arg is runner.database
    assert config_arg is memory_config


def test_disabled_flag_skips_monitor() -> None:
    memory_config = MemoryConfig(recall_drift_monitor_enabled=False)
    runner = _runner(memory_manager=object(), memory_config=memory_config)

    _start(runner)

    assert runner._recall_drift_task is None


def test_missing_memory_manager_skips_monitor() -> None:
    memory_config = MemoryConfig(recall_drift_monitor_enabled=True)
    runner = _runner(memory_manager=None, memory_config=memory_config)

    _start(runner)

    assert runner._recall_drift_task is None
