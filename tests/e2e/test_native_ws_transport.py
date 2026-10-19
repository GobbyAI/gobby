"""Live native WS transport: owner decisions, backend respawn, bounded failure, gterm.

The Rust suite proves the front door in isolation. These tests run the
supervised isolated daemon with its real Python backend, which only a live
process tree can show.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psutil
import pytest
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from gobby.identity import hash_password
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.users import LocalUserManager
from gobby.terminals.host_client import HostClient
from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    control_socket_path,
    control_token_path,
    write_pidfile,
)
from gobby.utils.break_glass import break_glass_path
from gobby.utils.local_token import issue_agent_api_token, read_managed_signing_key
from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    create_host_socket_dir,
    daemon_token,
    spawn_daemon_instance,
    stop_terminal_host,
)
from tests.e2e.native_ws_probe_bootstrap import RECORDS_FILE
from tests.e2e.test_external_terminal_attach import _gterm_bin_dir
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.e2e

TEST_EMAIL = "native-ws-e2e-user@gobby.local"
TEST_PASSWORD = "native-ws-e2e-password"
SESSION_COOKIE = "gobby_session"
# Browser connections get a fresh per-connection identity in this form (native_bridge.py).
LOCAL_WEB_ID = re.compile(r"local-web-[0-9a-f]{8}")
MACHINE_ID = "21000000-0000-4000-8000-000000000002"
E2E_PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
PROBE_RUNNER = "tests.e2e.native_ws_probe_bootstrap"

# A backend loss closes public sockets with one of two documented pairs
# (docs/contracts/native-ws-transport.md): the private leg ends, or a status
# change retires the generation first. Both are accepted; the test records which.
BACKEND_LOSS_CLOSES = {(1011, "Bridge unavailable"), (1012, "Service restart")}

# Identity, entry, path, generation, forwarding, and bridge headers. None may
# change who a connection is, on either entry.
FORGED_HEADERS = [
    ("X-Gobby-User-Id", str(uuid4())),
    ("X-Gobby-Machine-Id", str(uuid4())),
    ("X-Gobby-Key-Id", str(uuid4())),
    ("X-Gobby-Front-Door", "1"),
    ("X-Gobby-WS-Entry", "direct"),
    ("X-Gobby-WS-Path", "/ws"),
    ("X-Gobby-WS-Generation", uuid4().hex),
    ("X-Gobby-WS-Leg", "1"),
    ("X-Gobby-Backend-Token", "forged"),
    ("X-Forwarded-For", "203.0.113.7"),
    ("X-Forwarded-Host", "forged.example"),
    ("X-Forwarded-Proto", "https"),
]


@pytest.fixture
def e2e_pre_daemon_setup(postgres_db: HubDatabase) -> None:
    """Seed the canonical user so the browser cookie login succeeds."""
    users = LocalUserManager(postgres_db)
    users.update_profile(TEST_USER_ID, name="Native WS E2E User", email=TEST_EMAIL)
    users.update_password(TEST_USER_ID, hash_password(TEST_PASSWORD))


@pytest.fixture
def probe_daemon(
    e2e_project_dir: Path,
    e2e_config: tuple[Path, int, int],
    e2e_pre_daemon_setup: None,
) -> Iterator[DaemonInstance]:
    """A daemon whose backend records each successful bind, for the private-route probe."""
    _ = e2e_pre_daemon_setup
    yield from spawn_daemon_instance(e2e_project_dir, e2e_config, runner_module=PROBE_RUNNER)


@pytest.fixture
def probe_events(probe_daemon: DaemonInstance) -> Iterator[CLIEventSimulator]:
    events = CLIEventSimulator(probe_daemon.http_url, daemon_token(probe_daemon.gobby_home))
    yield events
    events.close()


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


async def _welcome(url: str, headers: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """Admit a client: the first frame is the welcome, and a ping gets a pong."""
    async with websockets.connect(
        url, additional_headers=headers, open_timeout=5.0, close_timeout=2.0
    ) as websocket:
        welcome: dict[str, Any] = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert welcome["type"] == "connection_established"
        await websocket.send(json.dumps({"type": "ping"}))
        pong = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert pong["type"] == "pong"
        return welcome


async def _connect(url: str, headers: list[tuple[str, str]]) -> ClientConnection:
    """Open an admitted socket and consume its welcome; the caller owns the close."""
    websocket = await websockets.connect(
        url, additional_headers=headers, open_timeout=5.0, close_timeout=2.0
    )
    welcome = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
    assert welcome["type"] == "connection_established"
    return websocket


async def _refused_status(url: str, headers: list[tuple[str, str]] | None = None) -> int:
    """Return the HTTP status of a handshake the front door refuses."""
    with pytest.raises(InvalidStatus) as raised:
        async with websockets.connect(
            url, additional_headers=headers, open_timeout=5.0, close_timeout=2.0
        ):
            pass
    return raised.value.response.status_code


async def _upgrade_close(url: str, headers: list[tuple[str, str]] | None = None) -> tuple[int, str]:
    """Return the (code, reason) of an upgraded connection the server closes."""
    async with websockets.connect(
        url, additional_headers=headers, open_timeout=5.0, close_timeout=2.0
    ) as websocket:
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(websocket.recv(), timeout=5.0)
    assert closed.value.rcvd is not None
    return closed.value.rcvd.code, closed.value.rcvd.reason


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


async def _frame_of_type(websocket: ClientConnection, frame_type: str) -> dict[str, Any]:
    while True:
        frame: dict[str, Any] = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        if frame.get("type") == frame_type:
            return frame


async def _attach(websocket: ClientConnection, session_id: str) -> None:
    await websocket.send(json.dumps({"type": "attach_to_session", "session_id": session_id}))
    result = await _frame_of_type(websocket, "attach_to_session_result")
    assert result["session_id"] == session_id


async def _hook_events_within(
    websocket: ClientConnection, seconds: float = 1.5
) -> list[dict[str, Any]]:
    """The hook_event frames that arrive inside a bounded window."""
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            frame = json.loads(await asyncio.wait_for(websocket.recv(), timeout=remaining))
        except TimeoutError:
            break
        if frame.get("type") == "hook_event":
            events.append(frame)
    return events


def _register_session(events: CLIEventSimulator) -> tuple[str, str]:
    """Register a terminal session; return its external id and its internal id."""
    external_id = f"native-ws-{uuid4().hex}"
    registered = events.register_session(
        external_id, machine_id=MACHINE_ID, project_id=E2E_PROJECT_ID
    )
    return external_id, str(registered["id"])


def _post_tool_use(events: CLIEventSimulator, external_id: str) -> None:
    result = events.post_tool_use(
        external_id,
        cli_source="claude",
        input_data={"tool_name": "Bash", "tool_input": {"command": "true"}},
        project_id=E2E_PROJECT_ID,
    )
    assert result.get("continue") is True, result


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


def _listens(pid: int, port: int) -> bool:
    """Whether the process holds a TCP listener on the port."""
    return any(
        conn.status == psutil.CONN_LISTEN and bool(conn.laddr) and conn.laddr.port == port
        for conn in psutil.Process(pid).net_connections(kind="inet")
    )


def _bind_records(daemon: DaemonInstance) -> list[dict[str, Any]]:
    path = daemon.gobby_home / RECORDS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


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
    operator_direct = await _welcome(direct, bearer)

    # Browser entry admits the session cookie; anonymous upgrades close 4401.
    assert (await _upgrade_close(browser))[0] == 4401
    operator_browser = await _welcome(browser, bearer)
    assert LOCAL_WEB_ID.fullmatch(operator_browser["user_id"]), operator_browser
    browser_user = (await _welcome(browser, session))["user_id"]
    assert LOCAL_WEB_ID.fullmatch(browser_user), browser_user

    # An invalid key refuses at the front door even beside a valid cookie.
    invalid = token[:-1] + ("A" if token[-1] != "A" else "B")
    invalid_bearer = [("Authorization", f"Bearer {invalid}")]
    assert await _refused_status(direct, invalid_bearer + session) == 401
    assert await _refused_status(browser, invalid_bearer + session) == 401

    # A revoked key is live before revocation and refused after it, beside the cookie.
    async with httpx.AsyncClient(base_url=daemon_instance.http_url, timeout=10.0) as client:
        minted = await client.post(
            "/api/auth/keys",
            json={"label": "native ws e2e revoked"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert minted.status_code == 200, minted.text
        issued = minted.json()
        revocable = [("Authorization", f"Bearer {issued['key']}")]
        await _welcome(direct, revocable)
        revoked = await client.delete(
            f"/api/auth/keys/{issued['key_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert revoked.status_code == 200, revoked.text
    assert await _refused_status(direct, revocable + session) == 401
    assert await _refused_status(browser, revocable + session) == 401

    # A managed agent bearer is never a principal, even beside a valid cookie.
    signing_key = read_managed_signing_key(daemon_instance.gobby_home / "bootstrap.yaml")
    assert signing_key is not None
    managed = issue_agent_api_token(
        signing_key,
        agent_run_id=str(uuid4()),
        session_id=str(uuid4()),
        project_id=E2E_PROJECT_ID,
        machine_id=MACHINE_ID,
    )
    managed_bearer = [("Authorization", f"Bearer {managed}")]
    assert await _refused_status(direct, managed_bearer + session) == 403
    assert (await _upgrade_close(browser, managed_bearer + session))[0] == 4401

    # Break-glass is ignored on the websocket scope: the cookie admits on browser only.
    break_glass = [
        ("X-Gobby-Break-Glass", break_glass_path(daemon_instance.gobby_home).read_text().strip())
    ]
    assert (await _upgrade_close(browser, break_glass))[0] == 4401
    assert await _refused_status(direct, break_glass) == 401
    await _welcome(browser, session + break_glass)
    assert await _refused_status(direct, session + break_glass) == 401

    # Forged identity, entry, path, generation, forwarding, and bridge headers
    # change nothing: direct reaches the operator identity, and browser reaches a fresh
    # local-web identity, never the forged value.
    for name, value in FORGED_HEADERS:
        for forged in (
            [(name, value)],
            [(name, value), (name, value)],
            [(name.swapcase(), value)],
        ):
            welcome_direct = await _welcome(direct, bearer + forged)
            assert welcome_direct["user_id"] == operator_direct["user_id"], name
            welcome_browser = await _welcome(browser, session + forged)
            assert LOCAL_WEB_ID.fullmatch(welcome_browser["user_id"]), name
            assert await _refused_status(direct, forged) == 401, name
            assert (await _upgrade_close(browser, forged))[0] == 4401, name

    # The private bridge and private proof routes are not public.
    async with httpx.AsyncClient(
        base_url=f"http://localhost:{daemon_instance.http_port}", timeout=10.0
    ) as client:
        assert (await client.get("/_gobby/ws/bridge")).status_code == 404
        assert (await client.post("/_gobby/ws/bind", json={})).status_code == 403
        assert (await client.post("/_gobby/ws/publish", json={})).status_code == 403


@pytest.mark.asyncio
async def test_backend_respawn_retires_generation_and_refuses_stale_proof(
    probe_daemon: DaemonInstance,
    probe_events: CLIEventSimulator,
    record_property: Callable[[str, object], None],
) -> None:
    token = daemon_token(probe_daemon.gobby_home)
    bearer = [("Authorization", f"Bearer {token}")]
    browser = f"ws://localhost:{probe_daemon.http_port}/ws"
    session = [("Cookie", f"{SESSION_COOKIE}={_browser_cookie(probe_daemon)}")]

    # The first bind comes from start, before any client, and it is the only one.
    (first_pid,) = _backend_pids(probe_daemon)
    before = _bind_records(probe_daemon)
    assert [record["pid"] for record in before] == [first_pid], before
    first = before[0]
    assert not _listens(first_pid, probe_daemon.ws_port), "the backend opened a WS listener"

    external_id, internal_id = _register_session(probe_events)
    direct_ws = await _connect(probe_daemon.ws_url, bearer)
    browser_ws = await _connect(browser, session)
    try:
        # Single fanout: one post-tool-use reaches each attached client exactly once.
        await _attach(direct_ws, internal_id)
        await _attach(browser_ws, internal_id)
        _post_tool_use(probe_events, external_id)
        direct_hooks, browser_hooks = await asyncio.gather(
            _hook_events_within(direct_ws), _hook_events_within(browser_ws)
        )
        assert len(direct_hooks) == 1, direct_hooks
        assert len(browser_hooks) == 1, browser_hooks

        # Backend loss closes each pre-kill socket promptly, with a documented pair.
        started = time.monotonic()
        psutil.Process(first_pid).kill()
        for label, websocket in (("direct", direct_ws), ("browser", browser_ws)):
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(websocket.recv(), timeout=5.0)
            assert closed.value.rcvd is not None
            pair = (closed.value.rcvd.code, closed.value.rcvd.reason)
            assert pair in BACKEND_LOSS_CLOSES, f"{label} closed with {pair}"
            assert time.monotonic() - started < 1.0, f"{label} backend-loss close not prompt"
            record_property(f"backend_loss_{label}", f"{pair[0]} {pair[1]}")
    finally:
        await direct_ws.close()
        await browser_ws.close()

    # Respawn rebinds with no manual bind: a new backend, and a new generation.
    await _wait_admitted(probe_daemon.ws_url, token)
    (second_pid,) = _backend_pids(probe_daemon)
    assert second_pid != first_pid
    records = _bind_records(probe_daemon)
    assert [record["pid"] for record in records] == [first_pid, second_pid], records
    second = records[1]
    assert second["generation"] != first["generation"]
    record_property("front_door_secret_constant", str(second["secret"] == first["secret"]))
    record_property("backend_token_rotated", str(second["token"] != first["token"]))

    async with httpx.AsyncClient(
        base_url=f"http://localhost:{probe_daemon.http_port}", timeout=10.0
    ) as client:
        # Retirement: the old generation publishes nothing, even with the current proof.
        retired = await client.post(
            "/_gobby/ws/publish",
            json={"generation": first["generation"], "ops": []},
            headers={
                "x-gobby-front-door": second["secret"],
                "x-gobby-backend-token": second["token"],
            },
        )
        assert retired.status_code == 409, retired.text
        # A bind proven with the retired backend's token is refused.
        stale = await client.post(
            "/_gobby/ws/bind",
            json={},
            headers={
                "x-gobby-front-door": first["secret"],
                "x-gobby-backend-token": first["token"],
            },
        )
        assert stale.status_code == 403, stale.text
    assert len(_bind_records(probe_daemon)) == 2, "a refused bind must record nothing"

    # The current generation still delivers exactly once to a fresh client.
    fresh = await _connect(probe_daemon.ws_url, bearer)
    try:
        await _attach(fresh, internal_id)
        _post_tool_use(probe_events, external_id)
        assert len(await _hook_events_within(fresh)) == 1
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_failure_reconnect_and_shutdown_are_bounded(daemon_instance: DaemonInstance) -> None:
    token = daemon_token(daemon_instance.gobby_home)
    bearer = [("Authorization", f"Bearer {token}")]
    await _wait_admitted(daemon_instance.ws_url, token)
    (backend,) = _backend_pids(daemon_instance)

    # Backend failure closes the public socket promptly, with a documented pair.
    # The oversize 1009 case is asserted by the Rust suite.
    async with websockets.connect(
        daemon_instance.ws_url, additional_headers=bearer, open_timeout=5.0, close_timeout=2.0
    ) as websocket:
        welcome = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert welcome["type"] == "connection_established"
        # Direct begins with an absent subscription state; this socket leaves a set behind.
        await websocket.send(
            json.dumps({"type": "subscribe", "events": ["hook_event", "terminal_output"]})
        )
        subscribed = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert subscribed["type"] == "subscribe_success"
        psutil.Process(backend).kill()
        started = time.monotonic()
        with pytest.raises(ConnectionClosed) as closed:
            await asyncio.wait_for(websocket.recv(), timeout=5.0)
        assert time.monotonic() - started < 1.0, "backend failure close was not prompt"
        assert closed.value.rcvd is not None
        assert (closed.value.rcvd.code, closed.value.rcvd.reason) in BACKEND_LOSS_CLOSES

    # A reconnect after the backend restart starts from the initial welcome.
    await _wait_admitted(daemon_instance.ws_url, token)
    await _welcome(daemon_instance.ws_url, bearer)

    # The reconnect also starts from the entry mode's initial subscription state. Direct
    # begins absent, so unsubscribing an event the dead socket held acknowledges an empty set.
    async with websockets.connect(
        daemon_instance.ws_url, additional_headers=bearer, open_timeout=5.0, close_timeout=2.0
    ) as websocket:
        welcome = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert welcome["type"] == "connection_established"
        await websocket.send(json.dumps({"type": "unsubscribe", "events": ["hook_event"]}))
        acked = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5.0))
        assert acked == {"type": "unsubscribe_success", "events": []}

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
    """The gterm host and its control socket survive daemon stop and restart."""
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


@pytest.mark.asyncio
async def test_running_pane_keeps_its_process_across_daemon_stop_and_restart(
    daemon_instance: DaemonInstance,
    gterm_host: tuple[Path, subprocess.Popen[bytes]],
    tmp_path: Path,
) -> None:
    """A running pane takes input and shows output across a daemon stop and restart.

    Input and output go straight to the gterm host's control socket. The native WS
    listener is not used during the pane steps, so the pane is driven without the
    daemon and checked on the same process group before, while stopped, and after.
    """
    socket_dir, host = gterm_host
    token = daemon_token(daemon_instance.gobby_home)
    client = await HostClient.connect(control_socket_path(socket_dir))
    try:
        await client.hello(
            CONTROL_PROTOCOL_VERSION, control_token_path(socket_dir).read_text().strip()
        )
        terminal_id = f"native-ws-{uuid4().hex[:8]}"
        spawn_key = uuid4().hex
        reserve_key = uuid4().hex
        reservation = await client.reserve_observer(terminal_id, reserve_key)
        prepared = await client.spawn(
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            reservation_id=reservation["reservation_id"],
            reserve_key=reservation["reserve_key"],
            argv=["/bin/sh"],
            cwd=str(tmp_path),
            env={"PATH": os.environ["PATH"]},
            cols=120,
            rows=40,
            commit_deadline_ms=5000,
        )
        await client.spawn_commit(terminal_id, spawn_key, 5000)
        host_terminal_id = str(prepared["host_terminal_id"])
        pgid = int(prepared["pgid"])

        async def drive(label: str, first: int, second: int) -> None:
            marker = tmp_path / f"{label}.marker"
            await client.write(
                host_terminal_id=host_terminal_id,
                kind="text",
                data=f"echo $(({first} + {second})); echo done > {marker}".encode(),
                submit=True,
            )
            await asyncio.to_thread(
                wait_for_condition,
                lambda: marker.exists(),
                timeout=10.0,
                interval=0.05,
                description=f"pane runs the {label} command",
            )
            snapshot = await client.snapshot(host_terminal_id, mode="text")
            assert str(first + second) in str(snapshot.get("text", "")), label
            os.killpg(pgid, 0)  # the pane's process group still exists
            assert host.poll() is None, "gterm host exited"

        await drive("live", 424242, 313131)
        await asyncio.to_thread(daemon_instance.stop)
        assert host.poll() is None, "gterm host exited with the daemon"
        await drive("stopped", 111111, 222222)
        await asyncio.to_thread(daemon_instance.restart)
        assert host.poll() is None, "gterm host exited across the daemon restart"
        await drive("restarted", 555555, 444444)
        await _wait_admitted(daemon_instance.ws_url, token)
    finally:
        await client.close()
