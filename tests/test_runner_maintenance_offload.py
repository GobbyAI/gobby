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
from gobby.runner_maintenance_recurring import metrics_archive_loop, metrics_cleanup_loop


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
@pytest.mark.parametrize(
    ("loop", "manager", "method_name", "expected_kwargs"),
    [
        (metrics_cleanup_loop, MagicMock(), "cleanup_old_metrics", {}),
        (
            metrics_archive_loop,
            MagicMock(),
            "archive_old_events",
            {"retention_days": 30},
        ),
    ],
)
async def test_recurring_metrics_work_uses_db_executor(
    loop: Callable[..., Any],
    manager: MagicMock,
    method_name: str,
    expected_kwargs: dict[str, Any],
) -> None:
    method = getattr(manager, method_name)
    method.return_value = 0
    run_db = RecordingDbRunner()
    shutdown = iter([False, True])

    await loop(
        manager,
        lambda: next(shutdown),
        run_db=run_db,
        startup_delay_seconds=0,
        sleep=AsyncMock(),
    )

    assert run_db.calls == [(method, (), expected_kwargs)]


@pytest.mark.asyncio
async def test_metric_snapshot_loop_uses_bounded_db_runner() -> None:
    storage = MagicMock()
    storage.delete_old_snapshots.return_value = 2
    run_db = RecordingDbRunner()
    shutdown = iter([False, True])

    with (
        patch("gobby.storage.metric_snapshots.MetricSnapshotStorage", return_value=storage),
        patch("gobby.telemetry.instruments.update_daemon_metrics") as update_metrics,
        patch(
            "gobby.telemetry.instruments.get_all_metrics",
            return_value={"requests": 1},
        ) as get_metrics,
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        await metric_snapshot_loop(
            MagicMock(),
            lambda: next(shutdown),
            run_db=run_db,
        )

    assert update_metrics.call_count == 1
    assert get_metrics.call_count == 1
    assert run_db.calls == [
        (storage.save_snapshot, ({"requests": 1},), {}),
        (
            storage.delete_old_snapshots,
            (),
            {
                "retention_hours": 24,
                "limit": _METRIC_SNAPSHOT_CLEANUP_BATCH_LIMIT,
            },
        ),
    ]
    assert storage.save_snapshot.call_count == 1
    assert storage.delete_old_snapshots.call_count == 1


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
