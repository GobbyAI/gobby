"""Live native WS transport: owner decisions, backend respawn, bounded failure, gterm.

The Rust suite proves the front door in isolation. These tests run the
supervised isolated daemon with its real Python backend, which only a live
process tree can show.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import httpx
import psutil
import pytest
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from gobby.identity import hash_password
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.users import LocalUserManager
from gobby.terminals.host_protocol import control_socket_path, control_token_path, write_pidfile
from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    DaemonInstance,
    create_host_socket_dir,
    daemon_token,
    stop_terminal_host,
)
from tests.e2e.test_external_terminal_attach import _gterm_bin_dir
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.e2e

TEST_EMAIL = "native-ws-e2e-user@gobby.local"
TEST_PASSWORD = "native-ws-e2e-password"
SESSION_COOKIE = "gobby_session"


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: HubDatabase) -> None:
    """Seed the canonical user so the browser cookie login succeeds."""
    users = LocalUserManager(postgres_db)
    users.update_profile(TEST_USER_ID, name="Native WS E2E User", email=TEST_EMAIL)
    users.update_password(TEST_USER_ID, hash_password(TEST_PASSWORD))


def _browser_cookie(daemon: DaemonInstance) -> str:
    with httpx.Client(base_url=daemon.http_url, timeout=10.0) as client:
        response = client.post(
            "/api/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD, "remember_me": False},
        )
        response.raise_for_status()
        cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie is not None
    return cookie


async def _welcome(url: str, headers: list[tuple[str, str]] | None = None) -> None:
    """Admit a client: the first frame is the welcome, and a ping gets a pong."""
    async with websockets.connect(
        url, additional_headers=headers, open_timeout=5.0, close_timeout=2.0
    ) as websocket:
        welcome = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert welcome["type"] == "connection_established"
        await websocket.send(json.dumps({"type": "ping"}))
        pong = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert pong["type"] == "pong"


async def _refused_status(url: str, headers: list[tuple[str, str]] | None = None) -> int:
    """Return the HTTP status of a handshake the front door refuses."""
    with pytest.raises(InvalidStatus) as raised:
        async with websockets.connect(
            url, additional_headers=headers, open_timeout=5.0, close_timeout=2.0
        ):
            pass
    return raised.value.response.status_code


async def _upgrade_close_code(url: str, headers: list[tuple[str, str]] | None = None) -> int:
    """Return the close code of an upgraded connection the server closes."""
    async with websockets.connect(
        url, additional_headers=headers, open_timeout=5.0, close_timeout=2.0
    ) as websocket:
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(websocket.recv(), timeout=5.0)
    assert closed.value.rcvd is not None
    return closed.value.rcvd.code


async def _admitted(url: str, token: str) -> bool:
    try:
        await _welcome(url, [("Authorization", f"Bearer {token}")])
    except (OSError, WebSocketException, TimeoutError):
        return False
    return True


async def _wait_admitted(url: str, token: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while not await _admitted(url, token):
        if time.monotonic() > deadline:
            pytest.fail(f"native WS at {url} did not admit within {timeout:.0f}s")
        await asyncio.sleep(0.25)


def _backend_pids(daemon: DaemonInstance) -> list[int]:
    """Python backends are gdaemon descendants launched through readiness_bootstrap."""
    pids: list[int] = []
    for child in psutil.Process(daemon.pid).children(recursive=True):
        try:
            if any("readiness_bootstrap" in part for part in child.cmdline()):
                pids.append(child.pid)
        except psutil.Error:
            continue
    return pids


@pytest.mark.asyncio
async def test_credential_ownership_and_private_channel_refusal(
    daemon_instance: DaemonInstance,
) -> None:
    token = daemon_token(daemon_instance.gobby_home)
    cookie = _browser_cookie(daemon_instance)
    direct = daemon_instance.ws_url
    browser = f"ws://localhost:{daemon_instance.http_port}/ws"
    bearer = [("Authorization", f"Bearer {token}")]
    session = [("Cookie", f"{SESSION_COOKIE}={cookie}")]

    # Direct entry admits the operator bearer key and nothing else.
    assert await _refused_status(direct) == 401
    assert await _refused_status(direct, [("Authorization", "Bearer not-a-real-key")]) == 401
    assert await _refused_status(direct, session) == 401
    await _welcome(direct, bearer)

    # Browser entry admits the session cookie; anonymous upgrades close 4401.
    assert await _upgrade_close_code(browser) == 4401
    await _welcome(browser, session)

    # Forged identity or entry headers stand in for no credential.
    forged = [
        ("X-Gobby-Front-Door", "1"),
        ("X-Gobby-Entry", "direct"),
        ("X-Gobby-User-Id", str(TEST_USER_ID)),
    ]
    assert await _refused_status(direct, forged) == 401

    # The private bridge and private proof routes are not public.
    async with httpx.AsyncClient(
        base_url=f"http://localhost:{daemon_instance.http_port}", timeout=10.0
    ) as client:
        assert (await client.get("/_gobby/ws/bridge")).status_code == 404
        assert (await client.post("/_gobby/ws/bind", json={})).status_code == 403
        assert (await client.post("/_gobby/ws/publish", json={})).status_code == 403


@pytest.mark.asyncio
async def test_backend_respawn_rebinds_and_single_fanout(daemon_instance: DaemonInstance) -> None:
    token = daemon_token(daemon_instance.gobby_home)
    await _wait_admitted(daemon_instance.ws_url, token)
    before = _backend_pids(daemon_instance)
    assert len(before) == 1, f"expected one Python backend, found {before}"
    psutil.Process(before[0]).kill()

    # Admission returns only after a live backend answers, with no manual bind.
    await _wait_admitted(daemon_instance.ws_url, token)
    after = _backend_pids(daemon_instance)
    assert len(after) == 1, f"expected one respawned backend, found {after}"
    assert after[0] != before[0]
    await _welcome(daemon_instance.ws_url, [("Authorization", f"Bearer {token}")])


@pytest.mark.asyncio
async def test_failure_reconnect_and_shutdown_are_bounded(daemon_instance: DaemonInstance) -> None:
    token = daemon_token(daemon_instance.gobby_home)
    bearer = [("Authorization", f"Bearer {token}")]
    await _wait_admitted(daemon_instance.ws_url, token)
    (backend,) = _backend_pids(daemon_instance)

    # Backend failure closes the public socket promptly with 1011, the code for
    # a private EOF. The oversize 1009 case is asserted by the Rust suite.
    async with websockets.connect(
        daemon_instance.ws_url, additional_headers=bearer, open_timeout=5.0, close_timeout=2.0
    ) as websocket:
        welcome = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert welcome["type"] == "connection_established"
        psutil.Process(backend).kill()
        started = time.monotonic()
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(websocket.recv(), timeout=5.0)
        assert time.monotonic() - started < 1.0, "backend failure close was not prompt"
        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == 1011

    # A reconnect after the backend restart starts from the initial welcome.
    await _wait_admitted(daemon_instance.ws_url, token)
    await _welcome(daemon_instance.ws_url, bearer)

    # Daemon stop closes the public socket with 1001 within one second.
    async with websockets.connect(
        daemon_instance.ws_url, additional_headers=bearer, open_timeout=5.0, close_timeout=2.0
    ) as websocket:
        welcome = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert welcome["type"] == "connection_established"
        started = time.monotonic()
        stopping = asyncio.create_task(asyncio.to_thread(daemon_instance.stop))
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(websocket.recv(), timeout=5.0)
        elapsed = time.monotonic() - started
        assert closed.value.rcvd is not None
        assert closed.value.rcvd.code == 1001
        assert elapsed < 1.0, f"daemon stop close took {elapsed:.2f}s"
        await stopping


def _control_socket_accepts(socket_dir: Path) -> bool:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        try:
            probe.connect(str(control_socket_path(socket_dir)))
        except OSError:
            return False
    return True


@pytest.fixture
def gterm_host() -> Iterator[tuple[Path, subprocess.Popen[bytes]]]:
    """A gterm host owned by the test, not by the daemon."""
    socket_dir = create_host_socket_dir()
    token_path = control_token_path(socket_dir)
    token_path.write_text(uuid4().hex)
    token_path.chmod(0o600)
    host = subprocess.Popen(
        [str(_gterm_bin_dir() / "gterm"), "host", "--socket-dir", str(socket_dir)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    write_pidfile(socket_dir, host.pid)
    try:
        wait_for_condition(
            lambda: control_socket_path(socket_dir).exists(),
            timeout=10.0,
            interval=0.05,
            description="gterm host control socket",
        )
        yield socket_dir, host
    finally:
        stop_terminal_host(socket_dir)
        if host.poll() is None:
            host.kill()
        host.wait(timeout=5.0)


def test_gterm_remains_available_without_daemon(
    daemon_instance: DaemonInstance,
    gterm_host: tuple[Path, subprocess.Popen[bytes]],
) -> None:
    """The gterm host and its control socket survive daemon stop and restart.

    This proves the host process and its direct control socket stay available
    without the native WS listener. It does not create a pane, so the running
    terminal half of B3.5 is covered by the host's survival, not by pane I/O.
    """
    socket_dir, host = gterm_host
    token = daemon_token(daemon_instance.gobby_home)
    assert _control_socket_accepts(socket_dir)

    daemon_instance.stop()
    assert host.poll() is None, "gterm host exited with the daemon"
    assert _control_socket_accepts(socket_dir)

    daemon_instance.restart()
    assert host.poll() is None, "gterm host exited across the daemon restart"
    assert _control_socket_accepts(socket_dir)
    asyncio.run(_wait_admitted(daemon_instance.ws_url, token))
