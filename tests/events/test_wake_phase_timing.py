"""Phase timings and log levels on the deferred-wake log line (#22879)."""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gobby.events.wake as wake_module
from gobby.events.wake import WakeDispatcher

SESSION_ID = "5d1f6c3a-2b7e-4c1d-9a8f-3e6b2c7d1a90"

# started, refreshed, reconciled, locked, finished
MARKS = [100.0, 100.5, 100.75, 101.0, 103.0]


def _dispatcher(
    monkeypatch: pytest.MonkeyPatch, result: dict[str, object]
) -> tuple[WakeDispatcher, AsyncMock]:
    refresh = AsyncMock(return_value=None)
    dispatcher = WakeDispatcher(
        session_manager=MagicMock(),
        ism_manager=MagicMock(),
        lifecycle_refresh=refresh,
    )
    monkeypatch.setattr(dispatcher, "_dispatch_live_wake_unlocked", AsyncMock(return_value=result))
    return dispatcher, refresh


def _wake_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    records = [r for r in caplog.records if r.getMessage().startswith("Deferred wake for session")]
    assert len(records) == 1
    return records[0]


@pytest.mark.asyncio
async def test_deferred_wake_line_reports_phases_that_sum_to_duration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    marks = iter(MARKS)
    monkeypatch.setattr(wake_module, "time", SimpleNamespace(monotonic=lambda: next(marks)))
    dispatcher, refresh = _dispatcher(
        monkeypatch, {"delivered": True, "method": "tmux", "skipped": None}
    )
    caplog.set_level(logging.DEBUG, logger="gobby.events.wake")

    await dispatcher._refresh_and_retry_wake(SESSION_ID, priority="normal")

    refresh.assert_awaited_once_with(SESSION_ID)
    message = _wake_record(caplog).getMessage()
    fields = {k: float(v) for k, v in re.findall(r"(\w+_ms)=([0-9.]+)", message)}
    assert fields == {
        "duration_ms": 3000.0,
        "refresh_ms": 500.0,
        "reconcile_ms": 250.0,
        "lock_wait_ms": 250.0,
        "dispatch_ms": 2000.0,
    }
    phases = ("refresh_ms", "reconcile_ms", "lock_wait_ms", "dispatch_ms")
    assert sum(fields[p] for p in phases) == pytest.approx(fields["duration_ms"], abs=0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "level"),
    [
        ({"delivered": False, "method": None, "skipped": "debounced"}, logging.DEBUG),
        ({"delivered": False, "method": None, "skipped": "session_active"}, logging.INFO),
        ({"delivered": True, "method": "tmux", "skipped": None}, logging.INFO),
    ],
    ids=["debounced-debug", "session-active-info", "delivered-info"],
)
async def test_deferred_wake_log_level_by_outcome(
    result: dict[str, object],
    level: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dispatcher, _ = _dispatcher(monkeypatch, result)
    caplog.set_level(logging.DEBUG, logger="gobby.events.wake")

    await dispatcher._refresh_and_retry_wake(SESSION_ID, priority="normal")

    record = _wake_record(caplog)
    assert record.levelno == level
    assert f"skipped={result['skipped']}" in record.getMessage()
