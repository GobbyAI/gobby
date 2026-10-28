"""Daemon health stays up while the model metadata fetch stalls."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Generator

import httpx
import pytest

from tests.e2e.conftest import DaemonInstance

pytestmark = pytest.mark.e2e

STALL_WINDOW_SECONDS = 30.0
TRICKLE_INTERVAL_SECONDS = 1.0
CONNECT_REPLY_SETTLE_SECONDS = 0.5
TIMEOUT_LOG_LINE = "Timed out fetching models from OpenRouter"
# A TLS handshake record header promising 16 KiB. The body never completes, so the
# client waits in the handshake until it gives up.
STALLED_HANDSHAKE_RECORD = b"\x16\x03\x03\x40\x00"


class _StalledTunnel:
    """HTTPS CONNECT proxy whose TLS handshake trickles and never completes.

    The trickle sends one byte per second, so no single read stalls past a per-read
    timeout. Only the fetch's total deadline can end the attempt.
    """

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.2)
        self.port: int = self._listener.getsockname()[1]
        self.opened = threading.Event()
        self.abandoned = threading.Event()
        self._stop = threading.Event()
        self._accepting = threading.Thread(target=self._accept_loop, daemon=True)

    def start(self) -> None:
        self._accepting.start()

    def stop(self) -> None:
        self._stop.set()
        self._accepting.join(timeout=5)
        self._listener.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._tunnel, args=(conn,), daemon=True).start()

    def _tunnel(self, conn: socket.socket) -> None:
        with conn:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            self.opened.set()
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            # Send the handshake header after the client has parsed the CONNECT reply;
            # sent with it, the header lands in the client's CONNECT buffer and is lost.
            time.sleep(CONNECT_REPLY_SETTLE_SECONDS)
            conn.sendall(STALLED_HANDSHAKE_RECORD)
            while not self._stop.is_set():
                time.sleep(TRICKLE_INTERVAL_SECONDS)
                try:
                    conn.sendall(b"\x00")
                except OSError:
                    # The client gave up on the fetch: its total deadline fired.
                    self.abandoned.set()
                    return


@pytest.fixture
def stalled_openrouter(monkeypatch: pytest.MonkeyPatch) -> Generator[_StalledTunnel]:
    tunnel = _StalledTunnel()
    tunnel.start()
    proxy = f"http://127.0.0.1:{tunnel.port}"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        yield tunnel
    finally:
        tunnel.stop()


def test_daemon_stays_healthy_while_model_fetch_stalls(
    stalled_openrouter: _StalledTunnel,
    daemon_instance: DaemonInstance,
    daemon_client: httpx.Client,
) -> None:
    """Startup reaches healthy while the fetch is stalled; the fetch ends at its deadline."""
    assert stalled_openrouter.opened.wait(timeout=STALL_WINDOW_SECONDS), "fetch never started"
    assert daemon_client.get("/api/auth/status").status_code == 200

    assert stalled_openrouter.abandoned.wait(timeout=STALL_WINDOW_SECONDS), "fetch never gave up"
    logs = daemon_instance.read_logs()
    assert TIMEOUT_LOG_LINE in logs, logs[-3000:]
    assert daemon_client.get("/api/auth/status").status_code == 200
