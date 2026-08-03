"""Tests for hook daemon readiness gating."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.health_gate import (
    DaemonNotReadyError,
    ensure_daemon_ready,
    ensure_daemon_ready_async,
)
from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent

pytestmark = pytest.mark.unit


class UnavailableHealthMonitor:
    """Health monitor stub that reports the daemon as unavailable."""

    def get_cached_status(self) -> tuple[bool, str | None, str, str | None]:
        return False, None, "not_running", "Connection refused"

    def check_now(self) -> bool:
        return False


class RefreshingHealthMonitor:
    """Health monitor whose fresh result updates an initially failed cache."""

    def __init__(self, fresh_status: tuple[bool, str | None, str, str | None]) -> None:
        self.status = (False, None, "not_running", "cached connection failure")
        self.fresh_status = fresh_status
        self.check_count = 0

    def get_cached_status(self) -> tuple[bool, str | None, str, str | None]:
        return self.status

    def check_now(self) -> bool:
        self.check_count += 1
        self.status = self.fresh_status
        return self.status[0]


def _event(event_type: HookEventType = HookEventType.BEFORE_TOOL) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="session-1",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        machine_id="21000000-0000-4000-8000-000000000001",
    )


@pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.AFTER_AGENT])
def test_planned_restart_marker_retains_terminal_hooks(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    event_type: HookEventType,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr("gobby.hooks.health_gate.RETRY_DELAYS", ())
    write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, home=tmp_path)
    logger = logging.getLogger("gobby.test.health_gate")
    caplog.set_level(logging.DEBUG, logger=logger.name)

    with pytest.raises(DaemonNotReadyError) as excinfo:
        ensure_daemon_ready(_event(event_type), UnavailableHealthMonitor(), logger)

    assert excinfo.value.daemon_status == "restarting (cli_restart)"
    assert excinfo.value.reason == "Connection refused"
    assert "Daemon unavailable during planned restart" in caplog.text
    assert all(record.levelno < logging.WARNING for record in caplog.records)


@pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.AFTER_AGENT])
def test_unexpected_unavailable_terminal_hook_blocks(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    event_type: HookEventType,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    monkeypatch.setattr("gobby.hooks.health_gate.RETRY_DELAYS", ())
    logger = logging.getLogger("gobby.test.health_gate")
    caplog.set_level(logging.WARNING, logger=logger.name)

    response = ensure_daemon_ready(_event(event_type), UnavailableHealthMonitor(), logger)

    assert response is not None
    assert response.decision == "block"
    assert response.reason is not None
    assert "Daemon not_running" in response.reason
    assert "Daemon not available after retries" in caplog.text


def test_unexpected_unavailable_daemon_still_warns(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    logger = logging.getLogger("gobby.test.health_gate")
    caplog.set_level(logging.WARNING, logger=logger.name)

    with pytest.raises(DaemonNotReadyError) as excinfo:
        ensure_daemon_ready(_event(), UnavailableHealthMonitor(), logger)

    assert excinfo.value.daemon_status == "not_running"
    assert excinfo.value.reason == "Connection refused"
    assert "Daemon not available after retries, retaining hook for replay" in caplog.text


def test_noncritical_hook_refreshes_failed_cache_before_allowing() -> None:
    monitor = RefreshingHealthMonitor((True, "Daemon ready", "ready", None))
    logger = logging.getLogger("gobby.test.health_gate")

    response = ensure_daemon_ready(_event(HookEventType.BEFORE_TOOL), monitor, logger)

    assert response is None
    assert monitor.check_count == 1


@pytest.mark.asyncio
async def test_async_noncritical_hook_refreshes_failed_cache_before_allowing() -> None:
    monitor = RefreshingHealthMonitor((True, "Daemon ready", "ready", None))
    logger = logging.getLogger("gobby.test.health_gate")

    response = await ensure_daemon_ready_async(_event(HookEventType.BEFORE_TOOL), monitor, logger)

    assert response is None
    assert monitor.check_count == 1


def test_noncritical_hook_uses_fresh_unhealthy_status_in_retry_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    monitor = RefreshingHealthMonitor((False, "Timed out", "cannot_access", "fresh health timeout"))
    logger = logging.getLogger("gobby.test.health_gate")
    caplog.set_level(logging.WARNING, logger=logger.name)

    with pytest.raises(DaemonNotReadyError) as excinfo:
        ensure_daemon_ready(_event(HookEventType.BEFORE_TOOL), monitor, logger)

    assert str(excinfo.value) == "Daemon cannot_access: fresh health timeout"
    assert "Status: cannot_access, Error: fresh health timeout" in caplog.text
    assert monitor.check_count == 1


def test_noncritical_hook_refreshes_once_before_planned_restart_retry(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, home=tmp_path)
    monitor = RefreshingHealthMonitor((False, "Timed out", "cannot_access", "fresh health timeout"))

    with pytest.raises(DaemonNotReadyError) as excinfo:
        ensure_daemon_ready(
            _event(HookEventType.BEFORE_TOOL),
            monitor,
            logging.getLogger("gobby.test.health_gate"),
        )

    assert str(excinfo.value) == "Daemon restarting (cli_restart): fresh health timeout"
    assert monitor.check_count == 1


def test_critical_hook_keeps_configured_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = RefreshingHealthMonitor((False, None, "not_running", "still down"))
    monkeypatch.setattr("gobby.hooks.health_gate.RETRY_DELAYS", (0, 0))

    response = ensure_daemon_ready(
        _event(HookEventType.STOP),
        monitor,
        logging.getLogger("gobby.test.health_gate"),
    )

    assert response is not None
    assert response.decision == "block"
    assert monitor.check_count == 2
