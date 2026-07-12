from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.runner_lifecycle_subsystems import _cleanup_metrics_on_startup
from gobby.runner_maintenance import (
    _APPROVAL_EXPIRY_BATCH_LIMIT,
    _COMMS_CLEANUP_BATCH_LIMIT,
    _METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT,
    cleanup_comms_messages_loop,
    expire_approval_timeouts_loop,
    metric_snapshot_loop,
)


class RecordingDbRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.calls.append((func, args, kwargs))
        return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_startup_metrics_cleanup_uses_db_executor() -> None:
    cleanup = MagicMock(return_value=4)
    run_db = RecordingDbRunner()
    runner = SimpleNamespace(
        metrics_manager=SimpleNamespace(cleanup_old_metrics=cleanup),
        db_executor=SimpleNamespace(run=run_db),
    )

    await _cleanup_metrics_on_startup(runner)

    assert run_db.calls == [(cleanup, (), {})]


@pytest.mark.asyncio
async def test_metric_snapshot_loop_uses_bounded_db_runner() -> None:
    storage = MagicMock()
    storage.delete_old_snapshots.return_value = 2
    run_db = RecordingDbRunner()
    shutdown = iter([False, True])

    with (
        patch("gobby.storage.metric_snapshots.MetricSnapshotStorage", return_value=storage),
        patch("gobby.telemetry.instruments.update_daemon_metrics"),
        patch("gobby.telemetry.instruments.get_all_metrics", return_value={"requests": 1}),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await metric_snapshot_loop(
            MagicMock(),
            lambda: next(shutdown),
            run_db=run_db,
        )

    assert [call[0] for call in run_db.calls] == [
        storage.save_snapshot,
        storage.delete_old_snapshots,
    ]
    storage.delete_old_snapshots.assert_called_once_with(
        retention_hours=24,
        limit=_METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT,
    )


@pytest.mark.asyncio
async def test_approval_expiry_loop_uses_bounded_db_runner() -> None:
    step = SimpleNamespace(id=11, execution_id=22, step_id="review")
    manager = MagicMock()
    manager.get_expired_approval_steps.return_value = [step]
    run_db = RecordingDbRunner()
    shutdown = iter([False, True])

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await expire_approval_timeouts_loop(
            manager,
            lambda: next(shutdown),
            run_db=run_db,
        )

    assert [call[0] for call in run_db.calls] == [
        manager.get_expired_approval_steps,
        manager.expire_approval_timeout,
    ]
    manager.get_expired_approval_steps.assert_called_once_with(limit=_APPROVAL_EXPIRY_BATCH_LIMIT)


@pytest.mark.asyncio
async def test_comms_cleanup_uses_db_runner_and_thread_with_bounds() -> None:
    store = MagicMock()
    store.delete_messages_before.return_value = 3
    attachment_manager = MagicMock()
    attachment_manager.cleanup_old.return_value = 2
    run_db = RecordingDbRunner()
    shutdown = iter([False, True])

    async def run_in_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    with (
        patch("gobby.storage.communications.LocalCommunicationsStore", return_value=store),
        patch(
            "gobby.communications.attachments.AttachmentManager", return_value=attachment_manager
        ),
        patch("asyncio.to_thread", new=AsyncMock(side_effect=run_in_thread)) as to_thread,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await cleanup_comms_messages_loop(
            MagicMock(),
            lambda: next(shutdown),
            run_db=run_db,
            startup_delay_seconds=0,
        )

    assert run_db.calls[0][0] == store.delete_messages_before
    assert run_db.calls[0][2]["limit"] == _COMMS_CLEANUP_BATCH_LIMIT
    to_thread.assert_awaited_once_with(
        attachment_manager.cleanup_old,
        days=30,
        limit=_COMMS_CLEANUP_BATCH_LIMIT,
    )
