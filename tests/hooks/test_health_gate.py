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


def _event() -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="session-1",
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={},
        machine_id="machine-1",
    )


def test_planned_restart_marker_downgrades_unavailable_warning(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, home=tmp_path)
    logger = logging.getLogger("gobby.test.health_gate")
    caplog.set_level(logging.DEBUG, logger=logger.name)

    response = ensure_daemon_ready(_event(), UnavailableHealthMonitor(), logger)

    assert response is not None
    assert response.decision == "allow"
    assert response.reason is not None
    assert "Daemon restarting (cli_restart)" in response.reason
    assert "Daemon unavailable during planned restart" in caplog.text
    assert all(record.levelno < logging.WARNING for record in caplog.records)


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
