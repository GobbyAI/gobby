"""Restart exit status against an isolated supervisor/backend HTTP boundary."""

import json
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli import daemon_lifecycle as lifecycle
from gobby.cli import daemon_start
from gobby.cli.runtime import CliRuntime
from gobby.config.app import DaemonConfig
from gobby.utils.env import E2E_ISOLATED_HOME_ENV


@dataclass
class RestartDaemon:
    port: int = 0
    becomes_ready: bool = True
    progress_requests: int = 0
    restart_requests: list[str] = field(default_factory=list)


@pytest.fixture
def restart_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[RestartDaemon]:
    state = RestartDaemon()

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/api/health":
                self.respond(200, {"status": "healthy"})
                return
            if self.path != "/api/admin/startup-progress":
                self.respond(404, {})
                return
            state.progress_requests += 1
            if state.progress_requests == 1 or not state.becomes_ready:
                self.respond(
                    503,
                    {"status": "unavailable", "backend": {"state": "starting", "target": "test"}},
                )
                return
            self.respond(
                200,
                {"done": state.progress_requests >= 3, "restart_recovery_pending": True},
            )

        def do_POST(self) -> None:
            state.restart_requests.append(self.path)
            self.respond(200, {"status": "restarting", "backend_pid": 42})

        def log_message(self, format: str, *args: object) -> None:
            """Keep the isolated server quiet; assertions inspect its recorded requests."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.port = server.server_port
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = MagicMock(spec=CliRuntime)
    runtime.read_only_operational_config.return_value = DaemonConfig(daemon_port=state.port)
    monkeypatch.setenv(E2E_ISOLATED_HOME_ENV, str(tmp_path))
    monkeypatch.setattr(lifecycle, "get_gobby_home", lambda: tmp_path)
    monkeypatch.setattr("gobby.cli.runtime.get_cli_runtime", lambda _ctx: runtime)
    monkeypatch.setattr(lifecycle, "restart_start_refusal", lambda _ctx: None)
    monkeypatch.setattr(lifecycle, "clear_protected_runs", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        lifecycle, "protect_pending_handoffs", lambda *_args, **_kwargs: nullcontext()
    )
    monkeypatch.setattr(lifecycle, "daemon_auth_headers", lambda: {})
    monkeypatch.setattr(lifecycle, "_is_process_alive", lambda _pid: False)
    elapsed = 0.0

    def advance(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(daemon_start, "time", SimpleNamespace(time=lambda: elapsed, sleep=advance))
    poll = daemon_start._poll_startup_progress
    monkeypatch.setattr(daemon_start, "_poll_startup_progress", lambda port: poll(port, max_wait=2))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("wait", [False, True], ids=["restart", "restart-wait"])
@pytest.mark.parametrize("becomes_ready", [True, False], ids=["recovers", "never-ready"])
def test_restart_waits_through_native_unavailability(
    restart_daemon: RestartDaemon, wait: bool, becomes_ready: bool
) -> None:
    restart_daemon.becomes_ready = becomes_ready
    result = CliRunner().invoke(lifecycle.restart, ["--wait"] if wait else [])

    assert result.exit_code == (0 if becomes_ready else 1), result.output
    assert restart_daemon.restart_requests == ["/api/admin/restart?force=true&terminals=false"]
    assert restart_daemon.progress_requests == (3 if becomes_ready else 4)
    if becomes_ready:
        assert "Backend did not finish startup readiness" not in result.output
    else:
        assert "Backend did not finish startup readiness" in result.output


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"status": "unavailable"},
        {"status": "unavailable", "backend": {"state": "starting"}},
        {"status": "unavailable", "backend": {"state": 1, "target": "test"}},
        {"status": "error", "backend": {"state": "starting", "target": "test"}},
        {
            "error": "backend_refused",
            "status": "unavailable",
            "backend": {
                "state": "refused",
                "refusal": "schema mismatch",
                "target": "http://127.0.0.1:60888",
            },
        },
        {
            "error": "standby",
            "status": "unavailable",
            "state": "standby",
            "backend": {"state": "standby", "target": "http://127.0.0.1:60888"},
        },
    ],
)
def test_startup_poll_rejects_unrecognized_503(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object] | None
) -> None:
    get = MagicMock(return_value=httpx.Response(503, json=payload))
    monkeypatch.setattr(httpx, "get", get)

    assert daemon_start._poll_startup_progress(1, max_wait=1) is False
    get.assert_called_once()
