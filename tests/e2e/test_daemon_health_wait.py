"""Tests for isolated-daemon health polling."""

import time
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import (
    DAEMON_HEALTH_MIN_PROBE_ATTEMPTS,
    DaemonHealthTimeoutError,
    find_free_port,
    wait_for_daemon_health,
)

pytestmark = pytest.mark.unit


def test_timed_out_probes_make_documented_minimum_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def time_out(_url: str, *, timeout: float) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout(f"probe timed out after {timeout}")

    monkeypatch.setattr("tests.e2e.conftest.httpx.get", time_out)

    with pytest.raises(DaemonHealthTimeoutError) as exc_info:
        wait_for_daemon_health(find_free_port(), timeout=0.0)

    assert attempts == DAEMON_HEALTH_MIN_PROBE_ATTEMPTS
    assert exc_info.value.attempts == DAEMON_HEALTH_MIN_PROBE_ATTEMPTS
    assert exc_info.value.timed_out == DAEMON_HEALTH_MIN_PROBE_ATTEMPTS


def test_failure_reports_probe_breakdown_and_daemon_log_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    attempts = 0
    log_file = tmp_path / "daemon.log"
    log_file.write_text("old startup line\nlatest startup progress\n")

    def fail_probe(url: str, *, timeout: float) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request = httpx.Request("GET", url)
        if attempts % 2:
            raise httpx.ConnectError("connection refused", request=request)
        raise httpx.ReadTimeout(f"probe timed out after {timeout}", request=request)

    monkeypatch.setattr("tests.e2e.conftest.httpx.get", fail_probe)

    with pytest.raises(DaemonHealthTimeoutError) as exc_info:
        wait_for_daemon_health(
            find_free_port(),
            log_file=log_file,
            timeout=0.0,
            min_attempts=4,
        )

    error = exc_info.value
    assert error.elapsed_seconds >= 0.0
    assert error.connect_refused == 2
    assert error.timed_out == 2
    message = str(error)
    assert f"after {error.elapsed_seconds:.3f}s" in message
    assert "attempts=4" in message
    assert "connect_refused=2" in message
    assert "timed_out=2" in message
    assert "--- daemon log tail ---" in message
    assert "latest startup progress" in message


def test_daemon_that_never_serves_route_fails_promptly(tmp_path: Path) -> None:
    log_file = tmp_path / "daemon.log"
    log_file.write_text("startup stopped making progress\n")
    started = time.monotonic()

    with pytest.raises(DaemonHealthTimeoutError) as exc_info:
        wait_for_daemon_health(
            find_free_port(),
            log_file=log_file,
            timeout=0.05,
            min_attempts=3,
        )

    assert time.monotonic() - started < 1.0
    assert exc_info.value.connect_refused >= 1
    assert "startup stopped making progress" in str(exc_info.value)
