"""Plan 5.2: external-session discovery and attach end-to-end."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import websockets

from gobby.storage.terminals import AttachLocator
from gobby.terminals.frame_client import FrameClient, FrameProtocolError
from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    daemon_token,
)

pytestmark = pytest.mark.e2e

E2E_PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"
OWNER_COLS, OWNER_ROWS = 120, 39
VIEWER_COLS, VIEWER_ROWS = 80, 24
APPROVAL_PROMPT = (
    "Tool call needs your approval.\n1. Allow / 2. Cancel\nPress Enter to approve this command\n"
)
PANE_PROPS = (
    "#{pane_pipe} #{alternate_on} #{pane_dead} #{cursor_flag} "
    "#{keypad_cursor_flag} #{bracket_paste_flag}"
)
OWNER_VIEW = (
    "#{session_name} #{window_id} #{pane_id} #{window_width} #{window_height} "
    "#{pane_width} #{pane_height} #{pane_active} #{window_active}"
)
_CLI_SCRIPT = f"""\
import sys
import termios

PROMPT = {APPROVAL_PROMPT!r}
fd = sys.stdin.fileno()
attrs = termios.tcgetattr(fd)
attrs[3] &= ~termios.ECHO
termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
sys.stdout.write("GOBBY-EXT-BOOT\\n")
sys.stdout.flush()
while True:
    cmd = sys.stdin.readline()
    if not cmd:
        break
    text = cmd.strip()
    if text == "SHOW_PROMPT":
        sys.stdout.write("\\n" * 80)
        sys.stdout.write(PROMPT)
        sys.stdout.write("GOBBY-EXT-READY\\n")
        sys.stdout.flush()
    elif text == "HIDE_CURSOR":
        sys.stdout.write("\\033[?25l")
        sys.stdout.flush()
    elif text == "APP_CURSOR":
        sys.stdout.write("\\033[?1h")
        sys.stdout.flush()
    elif text in {{"1", "2"}}:
        sys.stdout.write("ANSWERED:" + text + "\\n")
        sys.stdout.flush()
    else:
        sys.stdout.write("ECHO:" + text + "\\n")
        sys.stdout.flush()
"""


def _gterm_bin_dir() -> Path:
    env = os.environ.get("GOBBY_NATIVE_BIN_DIR")
    if env:
        return Path(env)
    worktree = Path(__file__).resolve().parents[2]
    for candidate in (
        worktree / "target" / "debug",
        worktree / ".gobby-native-bin",
        Path.home() / ".gobby" / "bin",
    ):
        if (candidate / "gterm").is_file():
            return candidate
    pytest.skip("gterm binary is not available")


@pytest.fixture
def e2e_pre_daemon_setup(
    postgres_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(_gterm_bin_dir()))
    socket_dir = Path(f"/tmp/gobby-host-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    socket_dir.mkdir(parents=True, exist_ok=True)
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch

    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(values={"terminal_host.socket_dir": str(socket_dir)}),
        source="e2e-external-terminal",
    )
    monkeypatch.setenv("GOBBY_E2E_HOST_SOCKET_DIR", str(socket_dir))
    try:
        yield
    finally:
        shutil.rmtree(socket_dir, ignore_errors=True)


def _tmux(socket: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["tmux", "-S", str(socket), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "tmux failed")
    return completed.stdout.strip()


class OwnerClient:
    """A live tmux client attached at a chosen PTY geometry."""

    def __init__(self, socket: Path, session: str, cols: int, rows: int) -> None:
        self.pid, self.fd = os.forkpty()
        if self.pid == 0:
            os.environ["TERM"] = "xterm-256color"
            os.execvp("tmux", ["tmux", "-S", str(socket), "attach-session", "-t", session])
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                os.read(self.fd, 4096)
            except OSError:
                return

    def resize(self, cols: int, rows: int) -> None:
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def close(self) -> None:
        self._stop.set()
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            return


class IsolatedTmux:
    """User-owned tmux server with a multi-window, multi-pane session."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.socket = Path(f"/tmp/gobby-ext-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
        self.session = f"ext-{uuid.uuid4().hex[:8]}"
        self.script = directory / "cli.py"
        self.script.write_text(_CLI_SCRIPT)
        self.owner: OwnerClient | None = None
        self.pane_id = ""
        self.control_pane = ""
        self.window_id = ""
        self.server_pid = 0
        self.start_time = 0
        self.session_name = self.session

    def start(self) -> OwnerClient:
        status = subprocess.run(
            [
                "tmux",
                "-S",
                str(self.socket),
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                self.session,
                "-x",
                "80",
                "-y",
                "24",
                "--",
                sys.executable,
                str(self.script),
            ],
            check=False,
        )
        if status.returncode != 0:
            raise RuntimeError("tmux new-session failed")
        self.pane_id = _tmux(self.socket, "display-message", "-p", "#{pane_id}")
        self.window_id = _tmux(self.socket, "display-message", "-p", "#{window_id}")
        self.server_pid = int(_tmux(self.socket, "display-message", "-p", "#{pid}"))
        self.start_time = int(_tmux(self.socket, "display-message", "-p", "#{start_time}"))
        _tmux(self.socket, "split-window", "-h", "-t", self.pane_id, "--", "/bin/sh")
        _tmux(self.socket, "select-pane", "-t", self.pane_id)
        _tmux(self.socket, "new-window", "-t", self.session, "--", "/bin/sh")
        self.control_pane = _tmux(
            self.socket, "display-message", "-p", "-t", f"{self.session}:1.0", "#{pane_id}"
        )
        _tmux(self.socket, "select-window", "-t", self.window_id)
        _tmux(self.socket, "select-pane", "-t", self.pane_id)
        owner = OwnerClient(self.socket, self.session, OWNER_COLS, OWNER_ROWS)
        self.owner = owner
        wait_for_condition(
            lambda: int(_tmux(self.socket, "display-message", "-p", "#{window_width}"))
            >= OWNER_COLS,
            timeout=5.0,
            interval=0.05,
            description="owner client geometry",
        )
        wait_for_condition(
            lambda: "GOBBY-EXT-BOOT" in self.capture(),
            timeout=5.0,
            interval=0.05,
            description="scripted CLI booted",
        )
        self.send_line("SHOW_PROMPT")
        wait_for_condition(
            lambda: "GOBBY-EXT-READY" in self.capture(),
            timeout=5.0,
            interval=0.05,
            description="prompt parked at pane tail",
        )
        return owner

    def context(self) -> dict[str, object]:
        return {
            "tmux_socket_path": str(self.socket),
            "tmux_pane": self.pane_id,
            "tmux_session": self.session_name,
            "tmux_window_id": self.window_id,
            "tmux_window": self.window_id,
            "tmux_server_pid": self.server_pid,
            "tmux_server_start_time": self.start_time,
        }

    def display(self, fmt: str, *, target: str | None = None) -> str:
        pane = target or self.pane_id
        return _tmux(self.socket, "display-message", "-p", "-t", pane, fmt)

    def clients(self) -> str:
        return _tmux(
            self.socket, "list-clients", "-F", "#{client_pid} #{client_width} #{client_height}"
        )

    def capture(self) -> str:
        return _tmux(self.socket, "capture-pane", "-p", "-e", "-t", self.pane_id)

    def send_line(self, text: str) -> None:
        hex_bytes = [f"{ord(ch):02x}" for ch in text]
        hex_bytes.append("0d")
        _tmux(self.socket, "send-keys", "-t", self.pane_id, "-H", *hex_bytes)

    def close(self) -> None:
        if self.owner is not None:
            self.owner.close()
            self.owner = None
        _tmux(self.socket, "kill-server", check=False)
        self.socket.unlink(missing_ok=True)


@pytest.fixture
def isolated_tmux(tmp_path: Path) -> Iterator[IsolatedTmux]:
    server = IsolatedTmux(tmp_path)
    server.start()
    try:
        yield server
    finally:
        server.close()


def _wait_for_host(client: httpx.Client, daemon: DaemonInstance | None = None) -> dict[str, Any]:
    last: dict[str, Any] = {}

    def snapshot() -> dict[str, Any]:
        response = client.get("/api/health")
        if response.status_code != 200:
            last["health_status"] = response.status_code
            last["health_body"] = response.text[:500]
            return {}
        payload = response.json()
        last["health"] = payload.get("gterm_host")
        last["degraded"] = payload.get("degraded_services")
        host = payload.get("gterm_host")
        if isinstance(host, dict) and host.get("running") and host.get("host_epoch"):
            return cast(dict[str, Any], host)
        return {}

    try:
        return wait_for_condition(snapshot, timeout=20.0, interval=0.2, description="gterm host")
    except AssertionError as exc:
        logs = ""
        if daemon is not None:
            logs = (
                f"\nlogs:\n{daemon.read_logs()[-2000:]}\nerror:\n{daemon.read_error_logs()[-2000:]}"
            )
            for relative in (Path(".gobby") / "logs" / "gterm.log", Path("logs") / "gterm.log"):
                gterm_log = daemon.gobby_home / relative
                if gterm_log.is_file():
                    logs += f"\ngterm {gterm_log}:\n{gterm_log.read_text()[-2000:]}"
        raise AssertionError(f"{exc}; last={last}{logs}") from exc


def _seed_session(
    cli_events: CLIEventSimulator,
    tmux: IsolatedTmux,
    *,
    cwd: str,
) -> str:
    """Register the external pane's CLI session and materialize its row.

    A startup SessionStart is stateless since #20968: the session row, and
    with it external terminal discovery, materializes on the first activity
    event, which carries the same terminal context.
    """
    external_id = f"ext-cli-{uuid.uuid4().hex[:8]}"
    terminal_context = tmux.context()
    cli_events.session_start(
        session_id=external_id,
        machine_id=MACHINE_ID,
        cli_source="claude",
        project_id=E2E_PROJECT_ID,
        cwd=cwd,
        terminal_context=terminal_context,
    )
    cli_events.user_prompt_submit(
        external_id,
        "SHOW_PROMPT",
        source="claude",
        machine_id=MACHINE_ID,
        cwd=cwd,
        project_id=E2E_PROJECT_ID,
        terminal_context=terminal_context,
    )
    return external_id


def _list_external(client: httpx.Client) -> dict[str, Any]:
    response = client.get("/api/terminals", params={"project_id": E2E_PROJECT_ID})
    response.raise_for_status()
    body = response.json()
    for item in body.get("items") or []:
        if item.get("ownership") == "external" and item.get("state") == "live":
            return cast(dict[str, Any], item)
    raise AssertionError(f"no live external terminal: {body}")


def _attach_from_item(item: dict[str, Any]) -> AttachLocator:
    attach = item.get("attach")
    assert isinstance(attach, dict)
    pid = attach.get("server_pid")
    start = attach.get("server_start_time")
    assert isinstance(pid, int)
    assert isinstance(start, int)
    backend = attach.get("backend")
    assert backend in {"tmux", "native"}
    return AttachLocator(
        backend=backend,
        frame_host_epoch=str(attach["frame_host_epoch"]),
        host_socket=str(attach["host_socket"]),
        socket_path=str(attach["socket_path"]),
        pane_id=str(attach["pane_id"]),
        server_pid=pid,
        server_start_time=start,
    )


def _frame_text(message: dict[str, Any]) -> str | None:
    if message.get("type") != "frame":
        return None
    width = int(message["width"])
    cells = message["cells"]
    rows: list[str] = []
    current: list[str] = []
    for index, cell in enumerate(cells):
        if index and index % width == 0:
            rows.append("".join(current))
            current = []
        current.append(str(cell.get("symbol") or ""))
    if current:
        rows.append("".join(current))
    return "\n".join(rows)


async def _open_viewer(
    locator: AttachLocator,
    token: str,
    *,
    cols: int,
    rows: int,
) -> FrameClient:
    assert locator.host_socket is not None
    host_dir = os.environ.get("GOBBY_E2E_HOST_SOCKET_DIR")
    frame_token = token
    if host_dir:
        token_path = Path(host_dir) / "local_cli_token"
        if token_path.is_file():
            frame_token = token_path.read_text(encoding="utf-8").strip()
        else:
            frame_token = ""
    reader, writer = await asyncio.open_unix_connection(locator.host_socket)
    client = FrameClient(reader, writer)
    await client.handshake(locator, local_token=frame_token, cols=cols, rows=rows)
    await client.attach_terminal(locator)
    try:
        first = await asyncio.wait_for(client.read_message(), timeout=5.0)
    except (FrameProtocolError, TimeoutError) as exc:
        host_dir = os.environ.get("GOBBY_E2E_HOST_SOCKET_DIR")
        extra = ""
        if host_dir:
            log_path = Path(host_dir) / "logs" / "gterm.log"
            if log_path.is_file():
                extra = f"\ngterm log:\n{log_path.read_text()[-4000:]}"
            pid_path = Path(host_dir) / "gterm.pid"
            if pid_path.is_file():
                extra += f"\npidfile={pid_path.read_text().strip()}"
                extra += (
                    "\nps="
                    + subprocess.run(
                        ["ps", "-p", pid_path.read_text().strip(), "-o", "args="],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stdout
                )
        raise AssertionError(f"attach read failed: {exc}; locator={locator}{extra}") from exc
    if first.get("type") == "error":
        raise AssertionError(f"attach refused: {first}")
    client._queue.append(first)
    return client


async def _read_until(
    client: FrameClient,
    predicate: Any,
    *,
    timeout: float,
    description: str,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = list(client._queue)
    client._queue.clear()
    for message in messages:
        if predicate(message):
            return messages
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            message = await asyncio.wait_for(client.read_message(), timeout=min(0.25, remaining))
        except TimeoutError:
            continue
        messages.append(message)
        if predicate(message):
            return messages
    brief = [
        {
            "type": message.get("type"),
            "width": message.get("width"),
            "height": message.get("height"),
            "code": message.get("code"),
            "cursor_visible": (message.get("modes") or {}).get("cursor_visible"),
            "keypad_cursor": (message.get("modes") or {}).get("keypad_cursor"),
            "pane_in_mode": (message.get("modes") or {}).get("pane_in_mode"),
        }
        for message in messages[-8:]
    ]
    raise AssertionError(f"timed out waiting for {description}: {brief}")


async def _ws_write(
    daemon: DaemonInstance,
    terminal_id: str,
    payload: str,
) -> None:
    token = daemon_token(daemon.gobby_home)
    async with websockets.connect(
        daemon.ws_url,
        additional_headers=[("Authorization", f"Bearer {token}")],
        open_timeout=5.0,
        close_timeout=2.0,
    ) as websocket:
        welcome = await asyncio.wait_for(websocket.recv(), timeout=5.0)
        assert isinstance(welcome, str)
        await websocket.send(
            json.dumps(
                {
                    "type": "terminal_attach",
                    "request_id": "ext-attach",
                    "terminal_id": terminal_id,
                    "frame_delivery": "direct",
                }
            )
        )
        attachment_id = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            parsed = json.loads(raw)
            if parsed.get("type") == "terminal_attach_result" and parsed.get("success"):
                attachment_id = parsed["attachment_id"]
                break
        assert isinstance(attachment_id, str)
        await websocket.send(
            json.dumps(
                {
                    "type": "terminal_take_control",
                    "terminal_id": terminal_id,
                    "attachment_id": attachment_id,
                    "takeover": True,
                }
            )
        )
        deadline = time.monotonic() + 5.0
        granted = False
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            parsed = json.loads(raw)
            if parsed.get("type") == "terminal_control_result":
                granted = bool(parsed.get("granted"))
                break
        assert granted is True
        await websocket.send(
            json.dumps(
                {
                    "type": "terminal_input",
                    "terminal_id": terminal_id,
                    "attachment_id": attachment_id,
                    "data": payload,
                    "client_write_seq": 1,
                }
            )
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(websocket.recv(), timeout=5.0)
            parsed = json.loads(raw)
            if parsed.get("type") == "terminal_write_outcome":
                assert parsed.get("outcome") == "delivered"
                return
        raise AssertionError("write outcome not received")


@pytest.mark.asyncio
async def test_external_discovery_attach_respond(
    daemon_instance: DaemonInstance,
    daemon_client: httpx.Client,
    cli_events: CLIEventSimulator,
    isolated_tmux: IsolatedTmux,
) -> None:
    _wait_for_host(daemon_client, daemon_instance)
    wait_for_condition(
        lambda: "GOBBY-EXT-READY" in isolated_tmux.capture(),
        timeout=5.0,
        interval=0.05,
        description="scripted CLI ready",
    )
    before = isolated_tmux.display(OWNER_VIEW)
    pipe_before = isolated_tmux.display("#{pane_pipe}")
    clients_before = isolated_tmux.clients()
    control_props = isolated_tmux.display(PANE_PROPS, target=isolated_tmux.control_pane)
    _seed_session(cli_events, isolated_tmux, cwd=str(daemon_instance.project_dir))
    item = wait_for_condition(
        lambda: _list_external(daemon_client),
        timeout=5.0,
        interval=0.1,
        description="external terminal row",
    )
    assert item["ownership"] == "external"
    locator = _attach_from_item(item)
    token = daemon_token(daemon_instance.gobby_home)
    viewer = await _open_viewer(locator, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
    frames = await _read_until(
        viewer,
        lambda message: (_frame_text(message) or "").find("GOBBY-EXT-READY") >= 0,
        timeout=8.0,
        description="ready marker in frames",
    )
    assert any("GOBBY-EXT-READY" in (_frame_text(message) or "") for message in frames)

    def roster_entry() -> dict[str, Any]:
        roster = daemon_client.get("/api/attention/roster")
        if roster.status_code != 200:
            return {}
        for entry in roster.json().get("entries") or []:
            attention = entry.get("attention") or {}
            if attention.get("fingerprint"):
                return cast(dict[str, Any], entry)
        return {}

    try:
        entry = wait_for_condition(
            roster_entry, timeout=25.0, interval=0.5, description="attention episode"
        )
    except AssertionError as exc:
        sessions = daemon_client.get("/api/sessions")
        roster = daemon_client.get("/api/attention/roster")
        raise AssertionError(
            f"{exc}; item={item}; sessions={sessions.text[:1500]}; "
            f"roster={roster.text[:1500]}; "
            f"pane_tail={isolated_tmux.capture()[-800]!r}; "
            f"logs={daemon_instance.read_logs()[-2500:]}"
        ) from exc
    raw_attention = entry.get("attention")
    assert isinstance(raw_attention, dict)
    entry_id = str(entry["entry_id"])
    respond = daemon_client.post(
        f"/api/attention/{entry_id}/respond",
        json={
            "attention_id": raw_attention["attention_id"],
            "fingerprint": raw_attention["fingerprint"],
            "answer": {"option": 1},
        },
    )
    assert respond.status_code == 200, respond.text
    wait_for_condition(
        lambda: "ANSWERED:" in isolated_tmux.capture(),
        timeout=5.0,
        interval=0.1,
        description="attention answer reached pane",
    )
    await viewer.detach()
    await viewer.close()
    assert isolated_tmux.display(OWNER_VIEW) == before
    assert isolated_tmux.display("#{pane_pipe}") == pipe_before == "0"
    assert isolated_tmux.display(PANE_PROPS, target=isolated_tmux.control_pane) == control_props
    assert isolated_tmux.clients() == clients_before
    _tmux(isolated_tmux.socket, "kill-window", "-t", isolated_tmux.window_id)
    wait_for_condition(
        lambda: daemon_client.get(f"/api/terminals/{item['id']}").json().get("state") == "exited",
        timeout=45.0,
        interval=1.0,
        description="liveness CAS exited the row",
    )


@pytest.mark.asyncio
async def test_external_owner_geometry_and_selection_preserved(
    daemon_instance: DaemonInstance,
    daemon_client: httpx.Client,
    cli_events: CLIEventSimulator,
    isolated_tmux: IsolatedTmux,
) -> None:
    _wait_for_host(daemon_client, daemon_instance)
    wait_for_condition(
        lambda: "GOBBY-EXT-READY" in isolated_tmux.capture(),
        timeout=5.0,
        interval=0.05,
        description="scripted CLI ready",
    )
    before = isolated_tmux.display(OWNER_VIEW)
    clients_before = isolated_tmux.clients()
    _seed_session(cli_events, isolated_tmux, cwd=str(daemon_instance.project_dir))
    item = wait_for_condition(
        lambda: _list_external(daemon_client),
        timeout=5.0,
        interval=0.1,
        description="external terminal row",
    )
    locator = _attach_from_item(item)
    token = daemon_token(daemon_instance.gobby_home)
    viewer = await _open_viewer(locator, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
    await _read_until(
        viewer,
        lambda message: (_frame_text(message) or "").find("GOBBY-EXT-READY") >= 0,
        timeout=8.0,
        description="discovered pane frames",
    )
    marker = f"IN-{uuid.uuid4().hex[:6]}"
    await _ws_write(daemon_instance, str(item["id"]), marker + "\r")
    wait_for_condition(
        lambda: marker in isolated_tmux.capture(),
        timeout=5.0,
        interval=0.1,
        description="input reached discovered pane",
    )
    assert isolated_tmux.display(OWNER_VIEW) == before
    assert isolated_tmux.display("#{pane_pipe}") == isolated_tmux.display(
        "#{pane_pipe}", target=isolated_tmux.control_pane
    )
    assert isolated_tmux.display("#{pane_pipe}") == "0"
    assert isolated_tmux.clients() == clients_before
    assert isolated_tmux.owner is not None
    before_pane = isolated_tmux.display("#{pane_width} #{pane_height}")
    isolated_tmux.owner.resize(100, 30)
    wait_for_condition(
        lambda: isolated_tmux.display("#{pane_width} #{pane_height}") != before_pane,
        timeout=5.0,
        interval=0.05,
        description="owner resize pane geometry",
    )
    resized_pane = isolated_tmux.display("#{pane_width} #{pane_height}")
    resized_window = isolated_tmux.display("#{window_width} #{window_height}")
    pane_width = int(resized_pane.split()[0])
    await _read_until(
        viewer,
        lambda message: message.get("type") == "frame"
        and int(message.get("width") or 0) == pane_width,
        timeout=8.0,
        description="owner resize on viewer frames",
    )
    assert isolated_tmux.display("#{window_width} #{window_height}") == resized_window
    terminal_id = str(item["id"])
    renamed = f"{isolated_tmux.session}-renamed"
    _tmux(isolated_tmux.socket, "rename-session", "-t", isolated_tmux.session_name, renamed)
    isolated_tmux.session_name = renamed
    detail = daemon_client.get(f"/api/terminals/{terminal_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == terminal_id
    clients_resized = isolated_tmux.clients()
    await viewer.set_viewport(40, 60)
    deadline = time.monotonic() + 0.4

    def owner_geometry_held() -> bool:
        assert isolated_tmux.display("#{window_width} #{window_height}") == resized_window
        assert isolated_tmux.clients() == clients_resized
        return time.monotonic() >= deadline

    wait_for_condition(
        owner_geometry_held,
        timeout=1.0,
        interval=0.05,
        description="viewport left owner geometry unchanged",
    )
    await viewer.detach()
    await viewer.close()
    assert isolated_tmux.display(OWNER_VIEW).split()[1] == before.split()[1]
    assert isolated_tmux.display("#{pane_pipe}") == "0"
    assert isolated_tmux.display("#{pane_pipe}", target=isolated_tmux.control_pane) == "0"


@pytest.mark.asyncio
async def test_modes_and_transient_failures_on_a_live_external_pane(
    daemon_instance: DaemonInstance,
    daemon_client: httpx.Client,
    cli_events: CLIEventSimulator,
    isolated_tmux: IsolatedTmux,
) -> None:
    _wait_for_host(daemon_client, daemon_instance)
    wait_for_condition(
        lambda: "GOBBY-EXT-READY" in isolated_tmux.capture(),
        timeout=5.0,
        interval=0.05,
        description="scripted CLI ready",
    )
    _seed_session(cli_events, isolated_tmux, cwd=str(daemon_instance.project_dir))
    item = wait_for_condition(
        lambda: _list_external(daemon_client),
        timeout=5.0,
        interval=0.1,
        description="external terminal row",
    )
    locator = _attach_from_item(item)
    token = daemon_token(daemon_instance.gobby_home)
    viewer = await _open_viewer(locator, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
    await _read_until(
        viewer,
        lambda message: message.get("type") == "frame",
        timeout=8.0,
        description="initial frame",
    )
    isolated_tmux.send_line("HIDE_CURSOR")
    hidden = await _read_until(
        viewer,
        lambda message: message.get("type") == "frame"
        and (
            (message.get("modes") or {}).get("cursor_visible") is False
            or (message.get("cursor") or {}).get("visible") is False
        ),
        timeout=8.0,
        description="hidden cursor",
    )
    last = hidden[-1]
    cells = last.get("cells")
    isolated_tmux.send_line("APP_CURSOR")
    modes = await _read_until(
        viewer,
        lambda message: message.get("type") == "frame"
        and bool((message.get("modes") or {}).get("keypad_cursor")),
        timeout=8.0,
        description="application cursor",
    )
    mode_frame = modes[-1]
    if cells is not None:
        assert mode_frame.get("cells") == cells or (mode_frame.get("modes") or {}).get(
            "keypad_cursor"
        )
    _tmux(isolated_tmux.socket, "copy-mode", "-t", isolated_tmux.pane_id)
    await _read_until(
        viewer,
        lambda message: (message.get("type") == "error" and message.get("code") == "copy_mode")
        or bool((message.get("modes") or {}).get("pane_in_mode")),
        timeout=8.0,
        description="copy-mode observation",
    )
    _tmux(
        isolated_tmux.socket, "send-keys", "-t", isolated_tmux.pane_id, "-X", "cancel", check=False
    )
    os.chmod(isolated_tmux.socket, 0o000)
    try:
        stale_messages = await _read_until(
            viewer,
            lambda message: message.get("type") == "error" and message.get("code") == "stale",
            timeout=8.0,
            description="stale indicator",
        )
        detail = daemon_client.get(f"/api/terminals/{item['id']}")
        assert detail.json()["state"] == "live"
        assert not any(message.get("type") == "terminal_exited" for message in stale_messages)
    finally:
        os.chmod(isolated_tmux.socket, 0o700)
    await _read_until(
        viewer,
        lambda message: message.get("type") == "frame",
        timeout=8.0,
        description="frames resume after transient failure",
    )
    still = daemon_client.get(f"/api/terminals/{item['id']}")
    assert still.json()["state"] == "live"
    await viewer.detach()
    await viewer.close()
