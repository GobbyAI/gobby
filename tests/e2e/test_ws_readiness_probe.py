"""The WebSocket readiness probe reports the last handshake failure, not a bare False."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import tests.e2e.conftest as e2e_conftest
from tests.e2e.conftest import daemon_websocket_failure_report, probe_daemon_websocket

pytestmark = [pytest.mark.unit]


class _RefusingHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def log_message(self, *args: object) -> None:
        """Keep the probe output quiet."""


@pytest.fixture
def refusing_port(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RefusingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(e2e_conftest, "daemon_token", lambda home: "test-token")
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def test_probe_failure_names_503_handshake_status(refusing_port: int, tmp_path: Path) -> None:
    failure = probe_daemon_websocket(refusing_port, tmp_path, timeout=0.5)

    assert failure is not None
    assert "503" in failure


def test_readiness_timeout_report_attaches_timings_and_log_tail(
    refusing_port: int, tmp_path: Path
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "startup-timings.jsonl").write_text('{"stage": "core_services", "seconds": 0.001}\n')
    daemon_log = logs / "daemon.log"
    daemon_log.write_text("".join(f"line {number}\n" for number in range(500)))
    error_log = logs / "daemon_error.log"
    error_log.write_text("startup refused\n")

    failure = probe_daemon_websocket(refusing_port, tmp_path, timeout=0.5)
    assert failure is not None
    report = daemon_websocket_failure_report(
        refusing_port, failure, tmp_path, daemon_log, error_log
    )

    assert "503" in report
    assert '"stage": "core_services"' in report
    assert "line 499\n" in report
    assert "line 299\n" not in report
    assert "startup refused" in report
