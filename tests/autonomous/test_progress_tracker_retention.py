"""Retention tests for autonomous loop progress telemetry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import MethodType, SimpleNamespace
from typing import cast

import pytest

from gobby.autonomous.progress_tracker import ProgressTracker
from gobby.runner_maintenance import telemetry_loops
from gobby.storage.hub.protocol import HubDatabase


class RecordingDatabase:
    """Minimal database fake that records cleanup SQL."""

    def __init__(self, *, rowcount: int = 0) -> None:
        self.rowcount = rowcount
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]) -> SimpleNamespace:
        self.calls.append((" ".join(sql.split()), params))
        return SimpleNamespace(rowcount=self.rowcount)


def test_prune_older_than_deletes_records_before_retention_cutoff() -> None:
    db = RecordingDatabase(rowcount=6)
    tracker = ProgressTracker(cast(HubDatabase, db))

    deleted = tracker.prune_older_than(retention_days=7)

    assert deleted == 6
    assert db.calls == [
        (
            "DELETE FROM loop_progress WHERE recorded_at < NOW() - (%s * INTERVAL '1 day')",
            (7,),
        )
    ]


@pytest.mark.asyncio
async def test_loop_progress_cleanup_runs_one_tick_and_logs_deleted_count(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[tuple[Callable[..., object], dict[str, object]]] = []

    async def run_db(func: Callable[..., object], **kwargs: object) -> int:
        calls.append((func, kwargs))
        return 4

    async def cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "gobby.runner_maintenance.telemetry_loops.asyncio.sleep",
        cancel_sleep,
    )

    with caplog.at_level(logging.INFO, logger="gobby.runner_maintenance"):
        await telemetry_loops.loop_progress_cleanup_loop(
            cast(HubDatabase, RecordingDatabase()),
            lambda: False,
            run_db=run_db,
        )

    assert len(calls) == 1
    method, kwargs = calls[0]
    assert isinstance(method, MethodType)
    assert isinstance(method.__self__, ProgressTracker)
    assert method.__name__ == "prune_older_than"
    assert kwargs == {"retention_days": 7}
    assert "Periodic loop progress cleanup: removed 4 old progress rows" in caplog.text


@pytest.mark.asyncio
async def test_loop_progress_cleanup_stops_before_tick_on_shutdown() -> None:
    called = False

    async def run_db(_func: Callable[..., object], **_kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    await telemetry_loops.loop_progress_cleanup_loop(
        cast(HubDatabase, RecordingDatabase()),
        lambda: True,
        run_db=run_db,
    )

    assert called is False


@pytest.mark.asyncio
async def test_loop_progress_cleanup_stops_on_cancelled_db_run() -> None:
    called = False

    async def run_db(_func: Callable[..., object], **_kwargs: object) -> int:
        nonlocal called
        called = True
        raise asyncio.CancelledError

    await telemetry_loops.loop_progress_cleanup_loop(
        cast(HubDatabase, RecordingDatabase()),
        lambda: False,
        run_db=run_db,
    )

    assert called is True
