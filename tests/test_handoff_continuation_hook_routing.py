"""CLI contract: real ghook routes only to the disposable HTTP receiver."""

import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from uuid import uuid4

import pytest

from tests.e2e.handoff_continuation_launch import provider_launch, seat_environment
from tests.native_binary_selection import select_native_binary

pytestmark = [pytest.mark.integration, pytest.mark.cli]


@contextmanager
def receiver() -> Iterator[tuple[int, Queue[dict[str, object]]]]:
    received: Queue[dict[str, object]] = Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.put({"path": self.path, "payload": json.loads(body)})
            self.respond()

        def do_GET(self) -> None:
            self.respond()

        def respond(self) -> None:
            body = b'{"continue":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port, received
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize(
    ("provider", "event"),
    [
        ("claude", "session-start"),
        ("codex", "SessionStart"),
        ("grok", "session_start"),
        ("agy", "SessionStart"),
        ("droid", "SessionStart"),
    ],
)
def test_native_hooks_override_ambient_daemon_url(
    tmp_path: Path, provider: str, event: str
) -> None:
    selected = select_native_binary("ghook", required=True)
    assert selected is not None
    binary = selected.path
    with receiver() as (foreign_port, foreign), receiver() as (isolated_port, isolated):
        ambient = {
            "HOME": str(tmp_path / "normal-home"),
            "GOBBY_DAEMON_URL": f"http://127.0.0.1:{foreign_port}",
        }
        env = seat_environment(ambient, home=tmp_path, port=isolated_port)
        env["GOBBY_PROJECT_ID"] = "00000000-0000-4000-8000-000000000001"
        command = provider_launch(
            [str(binary), "--gobby-owned", f"--cli={provider}", f"--type={event}"], env
        )
        result = subprocess.run(
            command,
            env=ambient,
            input=json.dumps({"session_id": str(uuid4()), "cwd": str(tmp_path)}),
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, f"{provider}: {result.stderr}"
        receipt = isolated.get(timeout=1)
        assert receipt["path"] == "/api/hooks/execute"
        assert foreign.empty(), f"{provider}: hook reached the ambient daemon endpoint"
