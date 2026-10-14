"""Tests for isolated-daemon health polling."""

import selectors
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import httpx
import psutil
import pytest

from tests.e2e.conftest import (
    DAEMON_HEALTH_LOG_TAIL_CHARS,
    DAEMON_HEALTH_MIN_PROBE_ATTEMPTS,
    DaemonHealthTimeoutError,
    find_free_port,
    wait_for_daemon_health,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("related", [True, False], ids=["descendant", "unrelated"])
def test_timeout_capture_targets_only_diagnostic_descendants(tmp_path: Path, related: bool) -> None:
    from tests.e2e.readiness_capture import capture_readiness_timeout

    child_script = """
import os, signal, sys, time
from pathlib import Path
logs = Path(sys.argv[1])
def threads(sig, frame):
    (logs / 'startup-threads.log').write_text('Thread: child_startup')
def tasks(sig, frame):
    (logs / 'startup-tasks.log').write_text('Task: child_startup')
    (logs / 'readiness-tasks.ready').write_text(str(os.getpid()))
signal.signal(signal.SIGUSR2, threads)
signal.signal(signal.SIGUSR1, tasks)
(logs / 'startup-timings.jsonl').write_text('child stage started')
(logs / 'readiness-diagnostics.pid').write_text(str(os.getpid()))
print('ready', flush=True)
time.sleep(30)
"""
    parent_script = "import subprocess, sys; subprocess.run(sys.argv[1:])"
    with (
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                parent_script,
                sys.executable,
                "-c",
                child_script,
                str(tmp_path),
            ],
            stdout=subprocess.PIPE,
        ) as parent,
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as other,
    ):
        child: psutil.Process | None = None
        try:
            marker = tmp_path / "readiness-diagnostics.pid"
            assert parent.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(parent.stdout, selectors.EVENT_READ)
                assert selector.select(timeout=5.0), "diagnostic child must announce readiness"
                assert parent.stdout.readline() == b"ready\n"
            assert marker.exists(), "diagnostic child must become ready"
            child = psutil.Process(int(marker.read_text()))
            capture = capture_readiness_timeout(tmp_path, parent if related else other)
            if related:
                assert "Thread: child_startup" in capture.thread_stack_tail
                assert "Task: child_startup" in capture.task_stack_tail
                assert (tmp_path / "readiness-tasks.ready").read_text() == str(child.pid)
            else:
                assert capture.thread_stack_tail == "<unavailable>"
                assert capture.task_stack_tail == "<unavailable>"
                assert child.is_running(), "unrelated diagnostic child must survive"
            assert capture.startup_timing_tail == "child stage started"
        finally:
            if child is not None and child.is_running():
                child.terminate()
            else:
                for descendant in psutil.Process(parent.pid).children(recursive=True):
                    descendant.terminate()
            parent.terminate()
            other.terminate()


def test_timed_out_probes_make_documented_minimum_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def time_out(_client: httpx.Client, _url: str, *, timeout: float) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout(f"probe timed out after {timeout}")

    monkeypatch.setattr("tests.e2e.conftest.httpx.Client.get", time_out)

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
    log_file.with_name("daemon_error.log").write_text("startup phase timed out\n")
    process = Mock(spec=subprocess.Popen)
    process.poll.return_value = 17

    def fail_probe(_client: httpx.Client, url: str, *, timeout: float) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        request = httpx.Request("GET", url)
        if attempts % 2:
            raise httpx.ConnectError("connection refused", request=request)
        raise httpx.ReadTimeout(f"probe timed out after {timeout}", request=request)

    monkeypatch.setattr("tests.e2e.conftest.httpx.Client.get", fail_probe)

    with pytest.raises(DaemonHealthTimeoutError) as exc_info:
        wait_for_daemon_health(
            find_free_port(),
            log_file=log_file,
            process=cast(subprocess.Popen[bytes], process),
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
    assert "process=exited(17)" in message
    assert "--- daemon log tail ---" in message
    assert "latest startup progress" in message
    assert "--- daemon error log tail ---" in message
    assert "startup phase timed out" in message


@pytest.mark.parametrize("mcp_log_state", ["populated", "empty", "missing"])
def test_unready_daemon_reports_bounded_mcp_startup_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mcp_log_state: str,
) -> None:
    log_file = tmp_path / "daemon.log"
    log_file.write_text("daemon still starting\n")
    mcp_log = tmp_path / "mcp.log"
    if mcp_log_state == "populated":
        mcp_log.write_text(
            "obsolete startup line\n" * DAEMON_HEALTH_LOG_TAIL_CHARS
            + "Startup step skills search started\n"
        )
    elif mcp_log_state == "empty":
        mcp_log.touch()

    def unavailable(_client: httpx.Client, url: str, *, timeout: float) -> httpx.Response:
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr("tests.e2e.conftest.httpx.Client.get", unavailable)
    with pytest.raises(DaemonHealthTimeoutError) as exc_info:
        wait_for_daemon_health(1, log_file=log_file, timeout=0.0, min_attempts=1)

    error = exc_info.value
    message = str(error)
    assert "last_status_code=503" in message
    assert "daemon still starting" in message
    assert "--- mcp log tail ---" in message
    if mcp_log_state == "populated":
        assert "Startup step skills search started" in message
        assert len(error.mcp_log_tail) == DAEMON_HEALTH_LOG_TAIL_CHARS
    elif mcp_log_state == "empty":
        assert f"<mcp log is empty: {mcp_log}>" in message
    else:
        assert f"<unable to read {mcp_log}:" in message


def test_daemon_that_never_serves_route_fails_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_file = tmp_path / "daemon.log"
    log_file.write_text("startup stopped making progress\n")
    elapsed = 0.0

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    def refused(_client: httpx.Client, url: str, *, timeout: float) -> httpx.Response:
        advance(0.02)
        raise httpx.ConnectError("isolated listener unavailable")

    monkeypatch.setattr(
        "tests.e2e.conftest.time", SimpleNamespace(monotonic=lambda: elapsed, sleep=advance)
    )
    monkeypatch.setattr("tests.e2e.conftest.httpx.Client.get", refused)

    with pytest.raises(DaemonHealthTimeoutError) as exc_info:
        wait_for_daemon_health(
            1,
            log_file=log_file,
            timeout=0.05,
            min_attempts=3,
        )

    assert exc_info.value.elapsed_seconds == pytest.approx(0.09)
    assert exc_info.value.attempts == 3
    assert exc_info.value.connect_refused == 3
    assert "startup stopped making progress" in str(exc_info.value)


def test_timeout_captures_isolated_stacks_timings_and_safe_backend_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_file = tmp_path / "daemon.log"
    log_file.write_text("registries initialized\n")
    process = Mock(spec=subprocess.Popen, pid=12345)
    process.poll.return_value = None
    (tmp_path / "readiness-diagnostics.pid").write_text(str(process.pid))
    (tmp_path / "startup-timings.jsonl").write_text(
        '{"stage":"transcript pool prewarm","state":"started"}\n'
    )

    def capture(sig: int) -> None:
        if sig == signal.SIGUSR2:
            (tmp_path / "startup-threads.log").write_text("Thread: blocked_startup\n")
        elif sig == signal.SIGUSR1:
            (tmp_path / "startup-tasks.log").write_text("Task: prewarm_transcript_evidence_pool\n")
            (tmp_path / "readiness-tasks.ready").write_text(str(process.pid))

    process.send_signal.side_effect = capture
    response = httpx.Response(
        503,
        json={
            "status": "unavailable",
            "backend": {"state": "starting", "target": "do-not-emit-target"},
            "token": "do-not-emit-token",
        },
    )
    monkeypatch.setattr("tests.e2e.conftest.httpx.Client.get", lambda *args, **kwargs: response)
    with pytest.raises(DaemonHealthTimeoutError) as exc_info:
        wait_for_daemon_health(
            find_free_port(),
            log_file=log_file,
            process=cast(subprocess.Popen[bytes], process),
            timeout=0.0,
            min_attempts=1,
        )
    error = exc_info.value
    assert error.backend_state == "starting"
    assert "blocked_startup" in error.thread_stack_tail
    assert "prewarm_transcript_evidence_pool" in error.task_stack_tail
    assert "transcript pool prewarm" in error.startup_timing_tail
    assert "do-not-emit" not in str(error)
    process.send_signal.assert_any_call(signal.SIGUSR2)
    process.send_signal.assert_any_call(signal.SIGUSR1)


@pytest.mark.parametrize("ready", [True, False])
def test_readiness_reuses_and_closes_loopback_client(
    monkeypatch: pytest.MonkeyPatch, ready: bool
) -> None:
    attempts = 0

    def probe(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        assert request.url == "http://127.0.0.1:1/api/auth/status"
        if attempts == 1:
            raise httpx.ConnectError("listener starting", request=request)
        return httpx.Response(200 if ready and attempts == 3 else 503, request=request)

    client = httpx.Client(transport=httpx.MockTransport(probe))
    factory = Mock(return_value=client)
    monkeypatch.setattr("tests.e2e.conftest.httpx.Client", factory)

    if ready:
        wait_for_daemon_health(1, timeout=0.0, min_attempts=3)
    else:
        with pytest.raises(DaemonHealthTimeoutError) as exc_info:
            wait_for_daemon_health(1, timeout=0.0, min_attempts=3)
        assert exc_info.value.connect_refused == 1
        assert exc_info.value.last_status_code == 503

    assert attempts == 3
    factory.assert_called_once_with(verify=False, trust_env=False)
    assert client.is_closed
