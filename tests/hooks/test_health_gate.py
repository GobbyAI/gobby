"""Tests for hook daemon readiness gating."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.health_gate import ensure_daemon_ready
from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent

pytestmark = pytest.mark.unit


class UnavailableHealthMonitor:
    """Health monitor stub that reports the daemon as unavailable."""

    def get_cached_status(self) -> tuple[bool, str | None, str, str | None]:
        return False, None, "not_running", "Connection refused"

    def check_now(self) -> bool:
        raise AssertionError("non-critical hook should not retry")


def _event(event_type: HookEventType = HookEventType.BEFORE_TOOL) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="session-1",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        machine_id="machine-1",
    )


@pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.AFTER_AGENT])
def test_planned_restart_marker_allows_terminal_hooks(
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

    response = ensure_daemon_ready(_event(event_type), UnavailableHealthMonitor(), logger)

    assert response is not None
    assert response.decision == "allow"
    assert response.reason is not None
    assert "Daemon restarting (cli_restart)" in response.reason
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

    response = ensure_daemon_ready(_event(), UnavailableHealthMonitor(), logger)

    assert response is not None
    assert response.decision == "allow"
    assert response.reason is not None
    assert "Daemon not_running" in response.reason
    assert "Daemon not available after retries" in caplog.text
