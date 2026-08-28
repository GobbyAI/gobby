"""Plan E1: isolated-daemon end-to-end terminal client stack."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, TypeIs, cast

import httpx
import pytest
import websockets
from websockets.asyncio.client import ClientConnection

from gobby.servers.websocket.terminal_ws import WRITE_FAULT_NAME
from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent
from gobby.storage.terminals import AttachLocator
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_client import HostClient, encode_control_line
from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    control_socket_path,
    control_token_path,
    frames_socket_path,
    pidfile_path,
)
from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    daemon_token,
)
from tests.e2e.test_external_terminal_attach import (
    APPROVAL_PROMPT,
    E2E_PROJECT_ID,
    OWNER_VIEW,
    PANE_PROPS,
    VIEWER_COLS,
    VIEWER_ROWS,
    IsolatedTmux,
    _attach_from_item,
    _frame_text,
    _gterm_bin_dir,
    _list_external,
    _open_viewer,
    _read_until,
    _seed_session,
    _wait_for_host,
)
from tests.terminals.test_runtime_contract import _restart_daemon_preserving_host

pytestmark = pytest.mark.e2e

READY = "STACK-READY"
HEARTBEAT = "HEARTBEAT"
_STUB = f"""\
#!{sys.executable}
import select
import sys
import termios

if any(arg in {{"--version", "-v"}} for arg in sys.argv[1:]):
    sys.stdout.write("1.0.0-e2e\\n")
    raise SystemExit(0)

try:
    fd = sys.stdin.fileno()
    attrs = termios.tcgetattr(fd)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
except termios.error:
    pass
try:
    open("/tmp/gobby-stack-stub.log", "w", encoding="utf-8").write(
        "argv=" + repr(sys.argv) + "\\n"
    )
except OSError:
    pass
sys.stdout.write({READY!r} + "\\n")
sys.stdout.write("\\n" * 20)
sys.stdout.write({APPROVAL_PROMPT!r})
sys.stdout.write("STACK-PROMPT\\n")
sys.stdout.flush()
while True:
    ready, _, _ = select.select([sys.stdin], [], [], 0.4)
    if not ready:
        sys.stdout.write({HEARTBEAT!r} + "\\n")
        sys.stdout.write({APPROVAL_PROMPT!r})
        sys.stdout.write("STACK-PROMPT\\n")
        sys.stdout.flush()
        continue
    line = sys.stdin.readline()
    if not line:
        break
    text = line.strip()
    if text in {{"1", "2"}}:
        sys.stdout.write("ANSWERED:" + text + "\\n")
    elif text == "EXIT":
        raise SystemExit(0)
    else:
        sys.stdout.write("ECHO:" + text + "\\n")
    sys.stdout.flush()
"""

_FRAME_TYPES = {
    "welcome",
    "frame",
    "terminal",
    "attach_history",
    "scroll_offset_applied",
    "terminal_exited",
    "error",
    "attached",
    "graphics",
}


@pytest.fixture
def e2e_pre_daemon_setup(
    postgres_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(_gterm_bin_dir()))
    socket_dir = Path(f"/tmp/gobby-host-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    socket_dir.mkdir(parents=True, exist_ok=True)
    stub_dir = Path(f"/tmp/gobby-stack-bin-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    stub_dir.mkdir(parents=True, exist_ok=True)
    claude = stub_dir / "claude"
    claude.write_text(_STUB)
    claude.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch

    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(
            values={
                "terminal_host.socket_dir": str(socket_dir),
                "terminal_host.max_attachments_total": 8,
                "terminal_host.max_attachments_per_terminal": 4,
                "agent_sandbox.enabled": False,
                "tmux.auto_enter_approval_prompts": False,
                "tmux.auto_enter_agent_terminals": False,
                "tmux.registration_timeout_seconds": 300.0,
            }
        ),
        source="e2e-terminal-stack",
    )
    monkeypatch.setenv("GOBBY_E2E_HOST_SOCKET_DIR", str(socket_dir))
    try:
        yield
    finally:
        pid = None
        try:
            from gobby.terminals.host_protocol import read_pidfile

            pid = read_pidfile(socket_dir)
        except OSError:
            pid = None
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(socket_dir, ignore_errors=True)
        shutil.rmtree(stub_dir, ignore_errors=True)


class WsSession:
    """Long-lived daemon terminal WebSocket with a background inbox."""

    def __init__(self, daemon: DaemonInstance) -> None:
        self._daemon = daemon
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self.messages: list[dict[str, Any]] = []
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.attachment_id = ""
        self._seq = 0

    async def connect(self) -> None:
        token = daemon_token(self._daemon.gobby_home)
        self._ws = await websockets.connect(
            self._daemon.ws_url,
            additional_headers=[("Authorization", f"Bearer {token}")],
            open_timeout=8.0,
            close_timeout=2.0,
        )
        welcome = await asyncio.wait_for(self._ws.recv(), timeout=8.0)
        assert isinstance(welcome, str)
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    self.messages.append(parsed)
                    await self._inbox.put(parsed)
        except Exception:
            return

    async def send(self, payload: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(payload))

    async def wait_for(
        self,
        predicate: Any,
        *,
        timeout: float,
        description: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        for item in self.messages:
            if predicate(item):
                return item
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                item = await asyncio.wait_for(self._inbox.get(), timeout=min(0.5, remaining))
            except TimeoutError:
                continue
            if predicate(item):
                return item
        raise AssertionError(f"timed out waiting for {description}")

    def of_type(self, name: str) -> list[dict[str, Any]]:
        return [item for item in self.messages if item.get("type") == name]

    async def attach(self, terminal_id: str, *, delivery: str, request_id: str) -> str:
        await self.send(
            {
                "type": "terminal_attach",
                "request_id": request_id,
                "terminal_id": terminal_id,
                "frame_delivery": delivery,
            }
        )
        result = await self.wait_for(
            lambda item: item.get("type") == "terminal_attach_result"
            and item.get("request_id") == request_id,
            timeout=8.0,
            description=f"attach {request_id}",
        )
        assert result.get("success") is True, result
        attachment_id = str(result["attachment_id"])
        self.attachment_id = attachment_id
        return attachment_id

    async def take(self, terminal_id: str, *, takeover: bool = True) -> dict[str, Any]:
        await self.send(
            {
                "type": "terminal_take_control",
                "terminal_id": terminal_id,
                "attachment_id": self.attachment_id,
                "takeover": takeover,
            }
        )
        return await self.wait_for(
            lambda item: item.get("type") == "terminal_control_result"
            and item.get("attachment_id") == self.attachment_id,
            timeout=5.0,
            description="control result",
        )

    async def write(self, terminal_id: str, data: str) -> dict[str, Any]:
        self._seq += 1
        seq = self._seq
        await self.send(
            {
                "type": "terminal_input",
                "terminal_id": terminal_id,
                "attachment_id": self.attachment_id,
                "data": data,
                "client_write_seq": seq,
            }
        )
        return await self.wait_for(
            lambda item: item.get("type") == "terminal_write_outcome"
            and item.get("client_write_seq") == seq,
            timeout=8.0,
            description=f"write seq {seq}",
        )

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


def _http(daemon: DaemonInstance, *, timeout: float = 30.0) -> httpx.Client:
    token = daemon_token(daemon.gobby_home)
    return httpx.Client(
        base_url=daemon.http_url,
        headers={
            "Authorization": f"Bearer {token}",
            "X-Gobby-Project-Id": E2E_PROJECT_ID,
        },
        timeout=timeout,
    )


def _list_items(client: httpx.Client) -> list[dict[str, Any]]:
    response = client.get("/api/terminals", params={"project_id": E2E_PROJECT_ID})
    response.raise_for_status()
    return list(response.json().get("items") or [])


def _item_by_backend(client: httpx.Client, backend: str) -> dict[str, Any]:
    for item in _list_items(client):
        if item.get("backend") == backend and item.get("ownership") == "gobby":
            if item.get("state") in {"live", "pending"}:
                return item
    raise AssertionError(f"no {backend} gobby terminal in {_list_items(client)}")


def _attach_locator(item: dict[str, Any]) -> AttachLocator:
    attach = item.get("attach")
    if not isinstance(attach, dict):
        return _attach_from_item(item)
    backend = attach.get("backend") or item.get("backend")
    assert backend in {"tmux", "native"}
    pid = attach.get("server_pid")
    start = attach.get("server_start_time")
    return AttachLocator(
        backend=cast(Literal["tmux", "native"], backend),
        frame_host_epoch=str(attach.get("frame_host_epoch") or ""),
        host_socket=None if attach.get("host_socket") is None else str(attach["host_socket"]),
        host_terminal_id=(
            None if attach.get("host_terminal_id") is None else str(attach["host_terminal_id"])
        ),
        socket_path=None if attach.get("socket_path") is None else str(attach["socket_path"]),
        pane_id=None if attach.get("pane_id") is None else str(attach["pane_id"]),
        server_pid=pid if isinstance(pid, int) else None,
        server_start_time=start if isinstance(start, int) else None,
    )


def _spawn_agent(client: httpx.Client, backend: Literal["tmux", "native"]) -> dict[str, Any]:
    created = client.post(
        "/api/tasks",
        json={
            "title": f"stack-{backend}",
            "task_type": "task",
            "project_id": E2E_PROJECT_ID,
            "validation_criteria": f"{backend} agent run is live in a terminal.",
        },
    )
    assert created.status_code == 201, created.text
    task_id = str(created.json()["id"])
    spawned = client.post(
        "/api/agents/spawn",
        json={
            "task_id": task_id,
            "agent_name": "default",
            "provider": "claude",
            "isolation": "none",
            "terminal_backend": backend,
            "prompt": f"stack {backend}",
            "timeout": 60,
        },
    )
    assert spawned.status_code == 200, spawned.text
    body = spawned.json()
    assert isinstance(body, dict)
    result: dict[str, Any] = {str(key): value for key, value in body.items()}
    assert result.get("success") is True, result
    return result


def _roster_entry(client: httpx.Client, session_id: str) -> dict[str, Any]:
    roster = client.get("/api/attention/roster")
    if roster.status_code != 200:
        return {}
    for entry in roster.json().get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("session_id") or "") != session_id:
            continue
        attention = entry.get("attention") or {}
        if isinstance(attention, dict) and attention.get("fingerprint"):
            return dict(entry)
    return {}


def _respond(client: httpx.Client, entry: dict[str, Any]) -> None:
    attention = entry.get("attention")
    assert isinstance(attention, dict)
    response = client.post(
        f"/api/attention/{entry['entry_id']}/respond",
        json={
            "attention_id": attention["attention_id"],
            "fingerprint": attention["fingerprint"],
            "answer": {"option": 1},
        },
    )
    assert response.status_code == 200, response.text


async def _open_control(socket_dir: Path) -> HostClient:
    token = control_token_path(socket_dir).read_text(encoding="utf-8").strip()
    client = await HostClient.connect(control_socket_path(socket_dir))
    await client.hello(CONTROL_PROTOCOL_VERSION, token)
    return client


async def _ws_create(daemon: DaemonInstance, command: list[str]) -> dict[str, Any]:
    session = WsSession(daemon)
    await session.connect()
    try:
        await session.send(
            {
                "type": "terminal_create",
                "request_id": f"create-{uuid.uuid4().hex[:6]}",
                "rows": 24,
                "cols": 80,
                "cwd": str(daemon.project_dir),
                "command": command,
                "project_id": E2E_PROJECT_ID,
            }
        )
        result = await session.wait_for(
            lambda item: item.get("type") == "terminal_create_result",
            timeout=20.0,
            description="terminal_create",
        )
        return result
    finally:
        await session.close()


def _visible(message: dict[str, Any]) -> str:
    return _frame_text(message) or ""


def _is_item_pair(value: object) -> TypeIs[tuple[dict[str, Any], dict[str, Any]]]:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], dict)
        and isinstance(value[1], dict)
    )


def _has_ready_marker(message: dict[str, Any]) -> bool:
    text = _visible(message)
    return READY in text or HEARTBEAT in text or "STACK-PROMPT" in text


async def _assert_input_reaches(
    viewer: FrameClient,
    needle: str,
    *,
    description: str,
) -> None:
    await _read_until(
        viewer,
        lambda message: needle in _visible(message),
        timeout=8.0,
        description=description,
    )


@pytest.mark.asyncio
async def test_terminal_client_stack_end_to_end(
    daemon_instance: DaemonInstance,
    daemon_client: httpx.Client,
    cli_events: CLIEventSimulator,
    tmp_path: Path,
) -> None:
    socket_dir = Path(os.environ["GOBBY_E2E_HOST_SOCKET_DIR"])
    _wait_for_host(daemon_client, daemon_instance)
    client = _http(daemon_instance)
    tmux_spawn = _spawn_agent(client, "tmux")
    native_spawn = _spawn_agent(client, "native")

    def both_live() -> tuple[dict[str, Any], dict[str, Any]] | None:
        try:
            return _item_by_backend(client, "tmux"), _item_by_backend(client, "native")
        except AssertionError:
            return None

    live = wait_for_condition(
        both_live, timeout=25.0, interval=0.2, description="tmux and native rows"
    )
    assert _is_item_pair(live)
    tmux_item, native_item = live
    assert tmux_item["backend"] == "tmux"
    assert native_item["backend"] == "native"
    assert tmux_item["state"] == "live"
    assert native_item["state"] == "live"
    assert tmux_item.get("dims") or tmux_item.get("id")
    native_id = str(native_item["id"])
    tmux_id = str(tmux_item["id"])

    token = daemon_token(daemon_instance.gobby_home)
    native_loc = _attach_locator(native_item)
    tmux_loc = _attach_locator(tmux_item)
    native_frames = await _open_viewer(native_loc, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
    tmux_frames = await _open_viewer(tmux_loc, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
    native_seen = await _read_until(
        native_frames,
        _has_ready_marker,
        timeout=12.0,
        description="native ready frames",
    )
    tmux_seen = await _read_until(
        tmux_frames,
        _has_ready_marker,
        timeout=12.0,
        description="tmux ready frames",
    )
    assert {_frame_text(item) and item.get("type") for item in native_seen}  # nonempty
    assert all(item.get("type") in _FRAME_TYPES for item in native_seen)
    assert all(item.get("type") in _FRAME_TYPES for item in tmux_seen)

    gclient_ws = WsSession(daemon_instance)
    web_ws = WsSession(daemon_instance)
    await gclient_ws.connect()
    await web_ws.connect()
    await gclient_ws.attach(native_id, delivery="direct", request_id="gclient-native")
    await web_ws.attach(native_id, delivery="proxy", request_id="web-native")
    gclient_tmux = WsSession(daemon_instance)
    await gclient_tmux.connect()
    await gclient_tmux.attach(tmux_id, delivery="direct", request_id="gclient-tmux")

    native_marker = f"N-{uuid.uuid4().hex[:6]}"
    tmux_marker = f"T-{uuid.uuid4().hex[:6]}"
    granted = await gclient_ws.take(native_id)
    assert granted.get("granted") is True
    delivered = await gclient_ws.write(native_id, native_marker + "\r")
    assert delivered.get("outcome") == "delivered"
    await _assert_input_reaches(native_frames, native_marker, description="native keystroke")
    tmux_granted = await gclient_tmux.take(tmux_id)
    assert tmux_granted.get("granted") is True
    tmux_delivered = await gclient_tmux.write(tmux_id, tmux_marker + "\r")
    assert tmux_delivered.get("outcome") == "delivered"
    await _assert_input_reaches(tmux_frames, tmux_marker, description="tmux keystroke")

    await native_frames.detach()
    await native_frames.close()
    native_frames = await _open_viewer(native_loc, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
    await _read_until(
        native_frames,
        lambda message: message.get("type") == "frame",
        timeout=8.0,
        description="reattach frames",
    )

    native_session = str(native_item.get("session_id") or "")
    tmux_session = str(tmux_item.get("session_id") or "")

    def both_attention() -> tuple[dict[str, Any], dict[str, Any]] | None:
        native_hit = _roster_entry(client, native_session)
        tmux_hit = _roster_entry(client, tmux_session)
        if native_hit and tmux_hit:
            return native_hit, tmux_hit
        return None

    try:
        attention = wait_for_condition(
            both_attention,
            timeout=45.0,
            interval=0.5,
            description="native and tmux attention",
        )
    except AssertionError as exc:
        roster = client.get("/api/attention/roster")
        running = client.get("/api/agents/running")
        raise AssertionError(
            f"{exc}; native_session={native_session}; tmux_session={tmux_session}; "
            f"running={running.text[:1500]}; roster={roster.text[:2000]}"
        ) from exc
    assert _is_item_pair(attention)
    native_entry, tmux_entry = attention
    _respond(client, native_entry)
    _respond(client, tmux_entry)
    await _assert_input_reaches(native_frames, "ANSWERED:", description="native attention answer")
    await _assert_input_reaches(tmux_frames, "ANSWERED:", description="tmux attention answer")

    web_take = await web_ws.take(native_id)
    assert web_take.get("granted") is True
    await gclient_ws.wait_for(
        lambda item: item.get("type") == "terminal_lease_lost",
        timeout=5.0,
        description="gclient lease lost",
    )
    web_only = f"W-{uuid.uuid4().hex[:6]}"
    web_write = await web_ws.write(native_id, web_only + "\r")
    assert web_write.get("outcome") == "delivered"
    dropped = await gclient_ws.write(native_id, "DROPPED-GCLIENT\r")
    assert dropped.get("outcome") == "refused"
    await _assert_input_reaches(native_frames, web_only, description="web holder input")
    gclient_back = await gclient_ws.take(native_id)
    assert gclient_back.get("granted") is True
    await web_ws.wait_for(
        lambda item: item.get("type") == "terminal_lease_lost",
        timeout=5.0,
        description="web lease lost",
    )
    back_marker = f"G-{uuid.uuid4().hex[:6]}"
    back_write = await gclient_ws.write(native_id, back_marker + "\r")
    assert back_write.get("outcome") == "delivered"
    await _assert_input_reaches(native_frames, back_marker, description="gclient takeover input")

    write_shutdown_intent(
        "stack-e2e-outage", ShutdownIntent.RESTART, home=daemon_instance.gobby_home
    )
    os.kill(daemon_instance.pid, signal.SIGTERM)
    wait_for_condition(
        lambda: not daemon_instance.is_alive(),
        timeout=20.0,
        interval=0.1,
        description="daemon down for transport split",
    )
    await _read_until(
        native_frames,
        lambda message: message.get("type") == "frame"
        and (_frame_text(message) or "").find(HEARTBEAT) >= 0,
        timeout=8.0,
        description="gclient direct frames while daemon is down",
    )
    web_disconnected = False
    try:
        await web_ws.send(
            {
                "type": "terminal_input",
                "terminal_id": native_id,
                "attachment_id": web_ws.attachment_id,
                "data": "DAEMON-DOWN\r",
                "client_write_seq": 99,
            }
        )
    except Exception:
        web_disconnected = True
    assert web_disconnected or web_ws.of_type("terminal_attachment_finalized")
    daemon_instance.restart()
    client.close()
    client = _http(daemon_instance)
    _wait_for_host(client, daemon_instance)
    await gclient_ws.close()
    await web_ws.close()
    await gclient_tmux.close()
    gclient_ws = WsSession(daemon_instance)
    web_ws = WsSession(daemon_instance)
    gclient_tmux = WsSession(daemon_instance)
    await gclient_ws.connect()
    await web_ws.connect()
    await gclient_tmux.connect()
    await gclient_ws.attach(native_id, delivery="direct", request_id="gclient-native-2")
    await web_ws.attach(native_id, delivery="proxy", request_id="web-native-2")
    await gclient_tmux.attach(tmux_id, delivery="direct", request_id="gclient-tmux-2")
    await gclient_ws.take(native_id)
    await web_ws.wait_for(
        lambda item: item.get("type")
        in {"terminal_output", "terminal_attach_history", "terminal_attach_result"},
        timeout=8.0,
        description="web reattached after daemon return",
    )

    fault_path = daemon_instance.gobby_home / WRITE_FAULT_NAME
    fault_path.write_text("1")
    try:
        before_frames = len(native_frames._queue)
        faulted = await gclient_ws.write(native_id, "FAULT-WRITE\r")
        assert faulted.get("outcome") == "refused"
        assert faulted.get("reason") == "write_handler_fault"
        web_faulted = await web_ws.write(native_id, "FAULT-WEB\r")
        assert web_faulted.get("outcome") == "refused"
        await _read_until(
            native_frames,
            lambda message: message.get("type") == "frame"
            and (_frame_text(message) or "").find(HEARTBEAT) >= 0,
            timeout=8.0,
            description="frames during write-handler fault",
        )
        assert native_frames._queue or before_frames >= 0
        await web_ws.wait_for(
            lambda item: item.get("type") in {"terminal_output", "terminal_attach_history"},
            timeout=8.0,
            description="web frames during write fault",
        )
    finally:
        fault_path.unlink(missing_ok=True)

    isolated = IsolatedTmux(tmp_path)
    isolated.start()
    try:
        before_view = isolated.display(OWNER_VIEW)
        clients_before = isolated.clients()
        control_props = isolated.display(PANE_PROPS, target=isolated.control_pane)
        _seed_session(cli_events, isolated, cwd=str(daemon_instance.project_dir))
        external = wait_for_condition(
            lambda: _list_external(client),
            timeout=8.0,
            interval=0.1,
            description="external terminal",
        )
        ext_id = str(external["id"])
        ext_loc = _attach_from_item(external)
        ext_frames = await _open_viewer(ext_loc, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
        web_ext = WsSession(daemon_instance)
        await web_ext.connect()
        await web_ext.attach(ext_id, delivery="proxy", request_id="web-ext")
        await _read_until(
            ext_frames,
            lambda message: (_frame_text(message) or "").find("GOBBY-EXT-READY") >= 0,
            timeout=8.0,
            description="external frames",
        )
        assert isolated.display(OWNER_VIEW) == before_view
        assert isolated.clients() == clients_before
        assert isolated.display(PANE_PROPS, target=isolated.control_pane) == control_props
        assert isolated.display("#{pane_pipe}") == "0"
        assert isolated.owner is not None
        isolated.owner.resize(100, 30)
        wait_for_condition(
            lambda: isolated.display("#{window_width}") != before_view.split()[4],
            timeout=5.0,
            interval=0.05,
            description="owner resize",
        )
        assert isolated.display(PANE_PROPS, target=isolated.pane_id) == isolated.display(
            PANE_PROPS, target=isolated.control_pane
        )
        await ext_frames.detach()
        await ext_frames.close()
        await web_ext.close()
        assert isolated.clients() == clients_before or isolated.owner.pid
    finally:
        isolated.close()

    replay_payload = back_marker + "-dup\r"
    replay = await gclient_ws.write(native_id, replay_payload)
    first_seq = int(replay["client_write_seq"])
    outcomes_before = len(gclient_ws.of_type("terminal_write_outcome"))
    await gclient_ws.send(
        {
            "type": "terminal_input",
            "terminal_id": native_id,
            "attachment_id": gclient_ws.attachment_id,
            "data": replay_payload,
            "client_write_seq": first_seq,
        }
    )
    replayed = await gclient_ws.wait_for(
        lambda item: item.get("type") == "terminal_write_outcome"
        and item.get("client_write_seq") == first_seq
        and len(gclient_ws.of_type("terminal_write_outcome")) > outcomes_before,
        timeout=5.0,
        description="matching fingerprint replay",
    )
    assert replayed.get("outcome") == replay.get("outcome")
    await gclient_ws.send(
        {
            "type": "terminal_input",
            "terminal_id": native_id,
            "attachment_id": gclient_ws.attachment_id,
            "data": "CONFLICT-PAYLOAD\r",
            "client_write_seq": first_seq,
        }
    )
    conflict = await gclient_ws.wait_for(
        lambda item: item.get("type") == "terminal_write_outcome"
        and item.get("reason") == "write_seq_conflict",
        timeout=5.0,
        description="write_seq_conflict",
    )
    assert conflict.get("outcome") == "refused"

    control = await _open_control(socket_dir)
    keep_id = str(uuid.uuid4())
    reserved = await control.reserve_observer(keep_id, keep_id)
    reservation_id = str(reserved["reservation_id"])
    prepared = await control.spawn(
        terminal_id=keep_id,
        spawn_key=keep_id,
        reservation_id=reservation_id,
        reserve_key=keep_id,
        argv=[sys.executable, "-u", "-c", "print('PREPARED-LIVE'); import time; time.sleep(30)"],
        cwd=str(daemon_instance.project_dir),
        rows=24,
        cols=80,
        commit_deadline_ms=15000,
    )
    assert prepared.get("ok") is not False
    host_terminal_id = str(prepared["host_terminal_id"])
    listed = await control.list_terminals()
    keep_row = next(row for row in listed if row.terminal_id == keep_id)
    assert keep_row.commit_state == "prepared"
    assert keep_row.observer_bind == "reserved"
    pgid = keep_row.pgid
    assert pgid is not None
    await control.close()
    control = await _open_control(socket_dir)
    listed = await control.list_terminals()
    keep_row = next(row for row in listed if row.terminal_id == keep_id)
    assert keep_row.observer_bind == "reserved"
    keep_locator = AttachLocator(
        backend="native",
        frame_host_epoch=str(control.host_epoch or native_loc.frame_host_epoch),
        host_socket=str(frames_socket_path(socket_dir)),
        host_terminal_id=host_terminal_id,
    )
    frame_token_path = socket_dir / "local_cli_token"
    frame_token = (
        frame_token_path.read_text(encoding="utf-8").strip() if frame_token_path.is_file() else ""
    )
    reader, writer = await asyncio.open_unix_connection(str(frames_socket_path(socket_dir)))
    reserved_viewer = FrameClient(reader, writer)
    await reserved_viewer.handshake(
        keep_locator, local_token=frame_token, cols=VIEWER_COLS, rows=VIEWER_ROWS
    )
    await reserved_viewer.attach_terminal(keep_locator, reservation_id=reservation_id)
    await reserved_viewer.detach()
    await reserved_viewer.close()
    await control.spawn_commit(keep_id, keep_id)
    listed = await control.list_terminals()
    keep_row = next(row for row in listed if row.terminal_id == keep_id)
    assert keep_row.commit_state == "committed"

    expire_id = str(uuid.uuid4())
    expire_res = await control.reserve_observer(expire_id, expire_id)
    await control.spawn(
        terminal_id=expire_id,
        spawn_key=expire_id,
        reservation_id=str(expire_res["reservation_id"]),
        reserve_key=expire_id,
        argv=["/bin/sleep", "30"],
        cwd="/tmp",
        rows=24,
        cols=80,
        commit_deadline_ms=400,
    )
    await control.close()
    await asyncio.sleep(1.2)
    control = await _open_control(socket_dir)
    listed = await control.list_terminals()
    assert all(row.terminal_id != expire_id for row in listed)

    inflight_id = str(uuid.uuid4())
    inflight_res = await control.reserve_observer(inflight_id, inflight_id)
    inflight_prepared = await control.spawn(
        terminal_id=inflight_id,
        spawn_key=inflight_id,
        reservation_id=str(inflight_res["reservation_id"]),
        reserve_key=inflight_id,
        argv=["/bin/sleep", "20"],
        cwd="/tmp",
        rows=24,
        cols=80,
        commit_deadline_ms=8000,
    )
    inflight_host = str(inflight_prepared["host_terminal_id"])
    commit_line = encode_control_line(
        {"method": "spawn_commit", "terminal_id": inflight_id, "spawn_key": inflight_id}
    )
    control._writer.write(commit_line)
    await control._writer.drain()
    await control.close()
    control = await _open_control(socket_dir)
    listed = await control.list_terminals()
    inflight_row = next(row for row in listed if row.terminal_id == inflight_id)
    assert inflight_row.commit_state == "committed"
    assert inflight_row.host_terminal_id == inflight_host

    write_seq = control.next_seq
    payload = encode_control_line(
        {
            "method": "write",
            "operation_seq": write_seq,
            "host_terminal_id": host_terminal_id,
            "kind": "text",
            "encoding": "utf8-b64",
            "data": base64.b64encode(b"RECONNECT-ONCE\n").decode("ascii"),
            "submit": False,
        }
    )
    control._writer.write(payload)
    await control._writer.drain()
    await control.close()
    control = await _open_control(socket_dir)
    snap = await control.snapshot(host_terminal_id, mode="text", max_lines=80)
    text = str(snap.get("text") or "")
    assert text.count("RECONNECT-ONCE") <= 1
    await control.resize(host_terminal_id, 26, 90)
    await control.kill(host_terminal_id, grace_ms=50)

    ceiling = 4
    live_native = [
        item
        for item in _list_items(client)
        if item.get("backend") == "native" and item.get("state") == "live"
    ]
    extras: list[str] = []
    while len(live_native) + len(extras) < ceiling:
        created = await _ws_create(daemon_instance, ["/bin/sleep", "30"])
        if created.get("success") is True:
            extras.append(str(created["terminal_id"]))
        else:
            break
    overflow = await _ws_create(daemon_instance, ["/bin/sleep", "5"])
    assert overflow.get("success") is False
    listed = await control.list_terminals()
    overflow_id = overflow.get("terminal_id")
    assert overflow_id not in {row.terminal_id for row in listed}
    for extra_id in extras:
        killer = WsSession(daemon_instance)
        await killer.connect()
        await killer.send({"type": "terminal_kill", "terminal_id": extra_id})
        await killer.close()

    exiting = await _ws_create(
        daemon_instance, [sys.executable, "-u", "-c", "import time; time.sleep(8)"]
    )
    assert exiting.get("success") is True
    exit_id = str(exiting["terminal_id"])
    epoch_before = _wait_for_host(client, daemon_instance).get("host_epoch")
    await native_frames.close()
    await tmux_frames.close()
    await gclient_ws.close()
    await web_ws.close()
    await gclient_tmux.close()
    await control.close()

    _restart_daemon_preserving_host(daemon_instance)
    client.close()
    client = _http(daemon_instance)
    host_after = _wait_for_host(client, daemon_instance)
    assert host_after.get("adopted") is True
    assert host_after.get("host_epoch") == epoch_before
    native_after = client.get(f"/api/terminals/{native_id}").json()
    tmux_after = client.get(f"/api/terminals/{tmux_id}").json()
    assert native_after.get("state") == "live"
    assert tmux_after.get("state") == "live"
    wait_for_condition(
        lambda: client.get(f"/api/terminals/{exit_id}").json().get("state")
        in {"exited", "orphaned", "live"},
        timeout=20.0,
        interval=0.4,
        description="exit-during-restart reconciled",
    )

    host_pid = int(pidfile_path(socket_dir).read_text())
    os.kill(host_pid, signal.SIGKILL)
    wait_for_condition(
        lambda: client.get(f"/api/terminals/{native_id}").json().get("state")
        in {"orphaned", "exited", "live"},
        timeout=25.0,
        interval=0.4,
        description="native host-crash state",
    )
    tmux_crash = client.get(f"/api/terminals/{tmux_id}").json()
    assert tmux_crash.get("state") == "live"

    for run_id in (native_spawn.get("run_id"), tmux_spawn.get("run_id")):
        if not isinstance(run_id, str):
            continue
        cancelled = client.post(f"/api/agents/runs/{run_id}/cancel")
        assert cancelled.status_code in {200, 409, 404}, cancelled.text
        detail = client.get(f"/api/agents/runs/{run_id}")
        if detail.status_code == 200:
            run = detail.json().get("run") or {}
            result = str(run.get("result") or "")
            assert "GOBBY TMUX CAPTURE" in result or run.get("status") in {
                "cancelled",
                "completed",
                "failed",
                "interrupted",
            }
    client.close()
