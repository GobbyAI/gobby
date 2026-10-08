"""Isolated terminal stack through Python protocol clients and real gclient PTYs."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, TypeIs, cast
from unittest.mock import patch

import httpx
import pytest
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from gobby.agents.detection.registry import DetectionManifestRegistry
from gobby.agents.idle_detector import (
    COMPOSER_PROBE_LINES,
    ComposerRead,
    IdleDetector,
    composer_text,
)
from gobby.agents.srt_runtime import SRT_PREFLIGHT_TIMEOUT_SECONDS
from gobby.servers.websocket.terminal_ws import WRITE_FAULT_NAME
from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent
from gobby.storage.terminals import AttachLocator, TerminalManager
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_client import CommitTransportError, HostClient, encode_control_line
from gobby.terminals.host_protocol import (
    CONTROL_PROTOCOL_VERSION,
    control_socket_path,
    control_token_path,
    frames_socket_path,
    pidfile_path,
)
from tests._timing import wait_for_awaited_condition, wait_for_condition
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    copy_daemon_api_key,
    create_host_socket_dir,
    daemon_token,
    link_operator_srt,
    stop_terminal_host,
)
from tests.e2e.gclient_driver import GclientDriver, Screen, in_prefix_mode
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
from pathlib import Path

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
session_id = sys.argv[sys.argv.index("--session-id") + 1]
# SRT permits writes under cwd; a shared /tmp log is outside that grant.
Path(f".gobby-stack-stub-{{session_id}}.log").write_text(
    "argv=" + repr(sys.argv) + "\\n", encoding="utf-8"
)
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
def e2e_home_dir(e2e_srt_spawn_home: Path) -> Path:
    return e2e_srt_spawn_home


@pytest.fixture
def e2e_pre_daemon_setup(
    postgres_db: Any,
    e2e_config: tuple[Path, int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(_gterm_bin_dir()))
    socket_dir = create_host_socket_dir()
    stub_dir = Path(tempfile.mkdtemp(prefix="gs-"))
    # A relocated host reads its frame credential from the socket directory.
    # Seed the same isolated credential for the daemon and the host before startup.
    daemon_home = e2e_config[0].parent
    # FrameClient resolves HOME/.gobby; the isolated daemon fixture sets HOME to
    # daemon_home while Rust resolves GOBBY_HOME directly.
    for directory in (daemon_home, daemon_home / ".gobby", socket_dir):
        directory.mkdir(exist_ok=True)
        copy_daemon_api_key(daemon_home, directory)
    link_operator_srt(daemon_home)
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
                "terminal_host.max_attachments_total": 64,
                "terminal_host.max_attachments_per_terminal": 8,
                "tmux.auto_enter_approval_prompts": False,
                "tmux.auto_enter_agent_terminals": False,
                # The stub never registers its child session; keep the
                # never-initialized kill beyond both daemon restarts.
                "tmux.init_timeout_seconds": 600,
            }
        ),
        source="e2e-terminal-stack",
    )
    monkeypatch.setenv("GOBBY_E2E_HOST_SOCKET_DIR", str(socket_dir))
    try:
        yield
    finally:
        stop_terminal_host(socket_dir)
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


def _live_native_items(
    client: httpx.Client, run_ids: tuple[str, str]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Wait for each accepted spawn's persisted terminal, ignoring unrelated rows."""
    terminal_ids: list[str] = []
    for run_id in run_ids:
        response = client.get(f"/api/agents/runs/{run_id}")
        assert response.status_code == 200, response.text
        run = response.json()["run"]
        assert run["status"] not in {"error", "timeout", "cancelled", "success"}, (
            f"run={run_id}; status={run['status']}; "
            f"error={str(run.get('error') or '')[:2000]}; "
            f"result={str(run.get('result') or '')[:2000]}"
        )
        terminal_id = run.get("terminal_id")
        if not isinstance(terminal_id, str):
            return None
        terminal_ids.append(terminal_id)
    live = {
        item["id"]: item
        for item in _list_items(client)
        if item.get("backend") == "native"
        and item.get("ownership") == "gobby"
        and item.get("state") == "live"
    }
    if not all(terminal_id in live for terminal_id in terminal_ids):
        return None
    return live[terminal_ids[0]], live[terminal_ids[1]]


@pytest.mark.parametrize("readiness", ["pending", "unpublished", "live"])
def test_native_readiness_matches_spawn_runs(readiness: str) -> None:
    """Unrelated live rows never satisfy readiness for the two requested runs."""

    def row(terminal_id: str) -> dict[str, str]:
        return {"id": terminal_id, "backend": "native", "ownership": "gobby", "state": "live"}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/terminals":
            items = [row("unrelated-one"), row("unrelated-two")]
            if readiness == "live":
                # Roster order must not change the association with each run.
                items.extend([row("terminal-second"), row("terminal-first")])
            return httpx.Response(200, json={"items": items})
        run_id = request.url.path.rsplit("/", 1)[-1]
        assert run_id in {"first", "second"}
        return httpx.Response(
            200,
            json={
                "run": {
                    "status": "running",
                    "terminal_id": None if readiness == "pending" else f"terminal-{run_id}",
                }
            },
        )

    with httpx.Client(
        base_url="http://stack.test", transport=httpx.MockTransport(respond)
    ) as client:
        live = _live_native_items(client, ("first", "second"))
    if readiness == "live":
        assert live == (row("terminal-first"), row("terminal-second"))
    else:
        assert live is None


def test_native_readiness_reports_failed_run() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/runs/first"
        return httpx.Response(200, json={"run": {"status": "error", "error": "spawn refused"}})

    with httpx.Client(
        base_url="http://stack.test", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(AssertionError, match="first.*error.*spawn refused"):
            _live_native_items(client, ("first", "second"))


def _stack_run_state(run: dict[str, Any]) -> dict[str, str]:
    return {
        key: str(run.get(key) or "")[:2000]
        for key in ("status", "terminal_reason", "resume_metadata_json")
    }


def _cancel_stack_run(client: httpx.Client, run_id: str) -> dict[str, Any] | None:
    cancelled = client.post(f"/api/agents/runs/{run_id}/cancel")
    detail = client.get(f"/api/agents/runs/{run_id}")
    run = detail.json()["run"] if detail.status_code == 200 else {}
    evidence = json.dumps(
        {
            "run_id": run_id,
            "cancel_status": cancelled.status_code,
            "cancel_response": cancelled.text[:2000],
            "detail_status": detail.status_code,
            **_stack_run_state(run),
        }
    )
    assert detail.status_code in {200, 404}, evidence
    if cancelled.status_code == 400:
        # Host-death reconciliation can cancel the run before cleanup gets here.
        # Verify both the exact refusal and the persisted state; reject other 400s.
        assert (
            cancelled.json().get("detail")
            == (f"Agent run '{run_id}' is not active (status=cancelled)")
            and run.get("status") == "cancelled"
        ), evidence
    else:
        assert cancelled.status_code in {200, 409, 404}, evidence
    if detail.status_code == 404:
        return None
    return {**run, "cleanup_http_status": cancelled.status_code}


@pytest.mark.parametrize(
    ("cancel_status", "cancel_detail", "run_status", "accepted"),
    [
        (200, "", "cancelled", True),
        (409, "", "cancelled", True),
        (404, "", None, True),
        (400, "Agent run 'first' is not active (status=cancelled)", "cancelled", True),
        (400, "Agent run 'first' is not active (status=cancelled)", "running", False),
        (400, "Agent run 'other' is not active (status=cancelled)", "cancelled", False),
        (400, "invalid cancellation request", "cancelled", False),
        (500, "cancellation failed", "cancelled", False),
    ],
)
def test_stack_cleanup_validates_already_cancelled_run(
    cancel_status: int, cancel_detail: str, run_status: str | None, accepted: bool
) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        if request.method == "POST":
            assert request.url.path == "/api/agents/runs/first/cancel"
            return httpx.Response(cancel_status, json={"detail": cancel_detail})
        assert request.url.path == "/api/agents/runs/first"
        if run_status is None:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "run": {
                    "status": run_status,
                    "terminal_reason": "daemon_stop",
                    "resume_metadata_json": {"retained": True},
                }
            },
        )

    with httpx.Client(
        base_url="http://stack.test", transport=httpx.MockTransport(respond)
    ) as client:
        if accepted:
            run = _cancel_stack_run(client, "first")
            if run_status is None:
                assert run is None
            else:
                assert run is not None and run["status"] == run_status
                assert run["cleanup_http_status"] == cancel_status
        else:
            with pytest.raises(AssertionError, match="daemon_stop"):
                _cancel_stack_run(client, "first")
    assert requests == ["POST", "GET"]


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


def _spawn_agent(client: httpx.Client) -> dict[str, Any]:
    """Spawn one agent run; agent spawn is native-only."""
    backend = "native"
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
            "checkout_mode": "none",
            "prompt": f"stack {backend}",
            # No run timeout: the agents must outlive both daemon restarts
            # until the test cancels them.
            "timeout": 0,
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


def _respond(client: httpx.Client, session_id: str) -> None:
    # The stub repaints every 0.4s, so the pane can move past the fingerprint
    # read from the roster before the answer lands. Respond refuses that with a
    # 409 naming the moved identity; re-read the current episode and answer it.
    def answered() -> httpx.Response | None:
        entry = _roster_entry(client, session_id)
        if not entry:
            return None
        attention = entry["attention"]
        response = client.post(
            f"/api/attention/{entry['entry_id']}/respond",
            json={
                "attention_id": attention["attention_id"],
                "fingerprint": attention["fingerprint"],
                "answer": {"option": 1},
            },
        )
        if response.status_code == 409 and response.json()["detail"]["code"] in {
            "prompt_changed",
            "stale_episode",
        }:
            return None
        return response

    response = wait_for_condition(
        answered, timeout=15.0, interval=0.5, description=f"answer for {session_id}"
    )
    assert response is not None
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
    # Two distinct native panes: the managed tmux runtime was retired
    # (#22932), so both seats exercise the one registered runtime.
    direct_spawn = _spawn_agent(client)
    native_spawn = _spawn_agent(client)

    run_ids = (direct_spawn["run_id"], native_spawn["run_id"])
    assert all(isinstance(run_id, str) for run_id in run_ids), (direct_spawn, native_spawn)

    def write_readiness_evidence(frames: list[dict[str, Any]]) -> Path:
        runs: list[dict[str, Any]] = []
        for run_id in run_ids:
            try:
                response = client.get(f"/api/agents/runs/{run_id}")
                response.raise_for_status()
                run = response.json()["run"]
                runs.append(
                    {
                        "run_id": run_id,
                        **{
                            key: str(run.get(key) or "")[:2000]
                            for key in ("error", "result", "terminal_id", "updated_at")
                        },
                        **_stack_run_state(run),
                    }
                )
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                runs.append({"run_id": run_id, "diagnostic_error": str(exc)[:2000]})
        logs: dict[str, str] = {}
        for label, path in (
            ("stdout", daemon_instance.log_file),
            ("stderr", daemon_instance.error_log_file),
            ("daemon", daemon_instance.gobby_home / "logs" / "daemon.log"),
            ("errors", daemon_instance.gobby_home / "logs" / "errors.log"),
        ):
            if path.is_file():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 8192))
                    logs[label] = stream.read(8192).decode("utf-8", errors="replace")
        evidence = tmp_path / "stack-readiness.json"
        evidence.write_text(json.dumps({"runs": runs, "daemon_logs": logs, "frames": frames}))
        return evidence

    # Spawn acceptance precedes native publication. Bound readiness by the run's
    # terminal_id plus its live roster row, with a two-minute integration deadline.
    try:
        live = wait_for_condition(
            lambda: _live_native_items(client, run_ids),
            timeout=120.0,
            interval=0.2,
            description=f"live native terminals for runs {run_ids}",
        )
    except (AssertionError, httpx.HTTPError) as exc:
        evidence = write_readiness_evidence([])
        raise AssertionError(f"{exc}; native-readiness evidence={evidence}") from exc
    assert _is_item_pair(live)
    direct_item, native_item = live
    assert direct_item["backend"] == "native"
    assert native_item["backend"] == "native"
    assert direct_item["state"] == "live"
    assert native_item["state"] == "live"
    native_id = str(native_item["id"])
    direct_id = str(direct_item["id"])
    assert direct_id != native_id
    token = daemon_token(daemon_instance.gobby_home)
    native_loc = _attach_locator(native_item)
    direct_loc = _attach_locator(direct_item)
    native_frames = await _open_viewer(native_loc, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
    direct_frames = await _open_viewer(direct_loc, token, cols=VIEWER_COLS, rows=VIEWER_ROWS)
    readiness_frames: list[dict[str, Any]] = []

    def ready(message: dict[str, Any]) -> bool:
        readiness_frames.append(
            {
                "type": message.get("type"),
                "width": message.get("width"),
                "height": message.get("height"),
                "text": _frame_text(message),
            }
        )
        del readiness_frames[:-8]
        return _has_ready_marker(message)

    # Native publication proves the SRT runner execed, not that Seatbelt has
    # compiled and started the CLI. Measured READY at ~57 s with 1376 write
    # denies; use SRT's 90 s preflight budget for this second compilation too.
    try:
        native_seen = await _read_until(
            native_frames,
            ready,
            timeout=SRT_PREFLIGHT_TIMEOUT_SECONDS,
            description="native ready frames",
        )
        direct_seen = await _read_until(
            direct_frames,
            ready,
            timeout=SRT_PREFLIGHT_TIMEOUT_SECONDS,
            description="direct ready frames",
        )
    except AssertionError as exc:
        evidence = write_readiness_evidence(readiness_frames)
        logs = sorted(path.name for path in daemon_instance.project_dir.glob(".gobby-stack-*.log"))
        raise AssertionError(
            f"{exc}; cwds={(direct_item.get('cwd'), native_item.get('cwd'))}; "
            f"logs={logs}; first-output evidence={evidence}"
        ) from exc
    for spawn, item in ((direct_spawn, direct_item), (native_spawn, native_item)):
        session_id = spawn["child_session_id"]
        assert isinstance(session_id, str), spawn
        cwd = item.get("cwd")
        assert isinstance(cwd, str), item
        assert Path(cwd).resolve() == daemon_instance.project_dir.resolve(), cwd
        log = daemon_instance.project_dir / f".gobby-stack-stub-{session_id}.log"
        assert log.is_file(), f"stub log missing after READY: {log}"
        contents = log.read_text()
        assert str(shutil.which("claude")) in contents, contents
        assert session_id in contents, contents
    assert {_frame_text(item) and item.get("type") for item in native_seen}  # nonempty
    assert all(item.get("type") in _FRAME_TYPES for item in native_seen)
    assert all(item.get("type") in _FRAME_TYPES for item in direct_seen)

    gclient_ws = WsSession(daemon_instance)
    web_ws = WsSession(daemon_instance)
    await gclient_ws.connect()
    await web_ws.connect()
    await gclient_ws.attach(native_id, delivery="direct", request_id="gclient-native")
    await web_ws.attach(native_id, delivery="proxy", request_id="web-native")
    gclient_direct = WsSession(daemon_instance)
    await gclient_direct.connect()
    await gclient_direct.attach(direct_id, delivery="direct", request_id="gclient-direct")

    native_marker = f"N-{uuid.uuid4().hex[:6]}"
    direct_marker = f"T-{uuid.uuid4().hex[:6]}"
    granted = await gclient_ws.take(native_id)
    assert granted.get("granted") is True
    delivered = await gclient_ws.write(native_id, native_marker + "\r")
    assert delivered.get("outcome") == "delivered"
    await _assert_input_reaches(native_frames, native_marker, description="native keystroke")
    direct_granted = await gclient_direct.take(direct_id)
    assert direct_granted.get("granted") is True
    direct_delivered = await gclient_direct.write(direct_id, direct_marker + "\r")
    assert direct_delivered.get("outcome") == "delivered"
    await _assert_input_reaches(direct_frames, direct_marker, description="direct keystroke")

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
    direct_session = str(direct_item.get("session_id") or "")

    def both_attention() -> tuple[dict[str, Any], dict[str, Any]] | None:
        native_hit = _roster_entry(client, native_session)
        direct_hit = _roster_entry(client, direct_session)
        if native_hit and direct_hit:
            return native_hit, direct_hit
        return None

    try:
        attention = wait_for_condition(
            both_attention,
            timeout=45.0,
            interval=0.5,
            description="native and direct attention",
        )
    except AssertionError as exc:
        roster = client.get("/api/attention/roster")
        running = client.get("/api/agents/running")
        raise AssertionError(
            f"{exc}; native_session={native_session}; direct_session={direct_session}; "
            f"running={running.text[:1500]}; roster={roster.text[:2000]}"
        ) from exc
    assert _is_item_pair(attention)
    _respond(client, native_session)
    _respond(client, direct_session)
    await _assert_input_reaches(native_frames, "ANSWERED:", description="native attention answer")
    await _assert_input_reaches(direct_frames, "ANSWERED:", description="direct attention answer")

    web_take = await web_ws.take(native_id)
    assert web_take.get("granted") is True, web_take
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
    assert web_ws._task is not None
    await asyncio.wait_for(web_ws._task, timeout=5.0)
    assert web_ws._ws is not None and web_ws._ws.close_code is not None
    daemon_instance.restart()
    client.close()
    client = _http(daemon_instance)
    _wait_for_host(client, daemon_instance)
    await gclient_ws.close()
    await web_ws.close()
    await gclient_direct.close()
    gclient_ws = WsSession(daemon_instance)
    web_ws = WsSession(daemon_instance)
    gclient_direct = WsSession(daemon_instance)
    await gclient_ws.connect()
    await web_ws.connect()
    await gclient_direct.connect()
    await gclient_ws.attach(native_id, delivery="direct", request_id="gclient-native-2")
    await web_ws.attach(native_id, delivery="proxy", request_id="web-native-2")
    await gclient_direct.attach(direct_id, delivery="direct", request_id="gclient-direct-2")
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
        await web_ws.wait_for(
            lambda item: item.get("type") in {"terminal_output", "terminal_attach_history"},
            timeout=8.0,
            description="web frames during write fault",
        )
    finally:
        fault_path.unlink(missing_ok=True)

    # External tmux discovery: a CLI session registered against a user-owned
    # tmux server still materializes an external row, and gobby must not touch
    # that server. The managed tmux runtime that used to serve the row's frames
    # was retired (#22932), so the daemon no longer attaches to it; only
    # discovery and the owner's untouched server are asserted here.
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
        assert external.get("backend") == "tmux"
        assert isolated.display(OWNER_VIEW) == before_view
        assert isolated.clients() == clients_before
        assert isolated.display(PANE_PROPS, target=isolated.control_pane) == control_props
        assert isolated.display("#{pane_pipe}") == "0"
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
    from gobby.utils.local_token import read_local_api_token

    frame_token = read_local_api_token(socket_dir / "bootstrap.yaml") or ""
    reader, writer = await asyncio.open_unix_connection(str(frames_socket_path(socket_dir)))
    reserved_viewer = FrameClient(reader, writer)
    await reserved_viewer.handshake(
        keep_locator, local_token=frame_token, cols=VIEWER_COLS, rows=VIEWER_ROWS
    )
    await reserved_viewer.attach_terminal(keep_locator, reservation_id=reservation_id)
    await reserved_viewer.detach()
    await reserved_viewer.close()
    await control.spawn_commit(keep_id, keep_id, 15000)
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
    control = await _open_control(socket_dir)

    async def prepared_row_expired() -> bool:
        return all(row.terminal_id != expire_id for row in await control.list_terminals())

    await wait_for_awaited_condition(
        prepared_row_expired,
        timeout=5.0,
        interval=0.1,
        description="prepared row past its commit deadline expired from list",
    )

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
    original_read_payload = control.read_payload

    async def drop_commit_reply() -> dict[str, Any]:
        await original_read_payload()
        raise ConnectionError("commit reply lost")

    reader_task = control._reader_task
    assert reader_task is not None
    reader_task.cancel()
    await asyncio.gather(reader_task, return_exceptions=True)
    with patch.object(control, "read_payload", new=drop_commit_reply):
        control._reader_task = asyncio.create_task(control._reader_loop(control._generation))
        with pytest.raises(CommitTransportError) as commit_error:
            await control.spawn_commit(inflight_id, inflight_id, 8000)
    assert commit_error.value.request_written is True
    await control.close()
    control = await _open_control(socket_dir)
    listed = await control.list_terminals()
    inflight_row = next(row for row in listed if row.terminal_id == inflight_id)
    assert inflight_row.commit_state == "committed"
    assert inflight_row.host_terminal_id == inflight_host
    # Its short-lived child would otherwise free an entitlement mid-fill below.
    await control.kill(inflight_host, grace_ms=50)

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

    # Native capacity is enforced at reserve time against the configured
    # attachment ceiling less the four reserved lifecycle slots
    # (gterminal host/native_ops.rs::reserve_observer ->
    # native_entitlement_ceiling() = max_attachments_total - 4), so fill the
    # host until a create is refused and prove the refusal is that gate. Every
    # child outlives the fill: an exit frees its entitlement, so the host's own
    # bound rows, not the daemon listing, prove the ceiling.
    entitlement_ceiling = 60  # terminal_host.max_attachments_total 64 - 4 reserved
    extras: list[str] = []
    overflow: dict[str, Any] = {}
    for _ in range(entitlement_ceiling + 1):
        overflow = await _ws_create(daemon_instance, ["/bin/sleep", "300"])
        if overflow.get("success") is not True:
            break
        extras.append(str(overflow["terminal_id"]))
    assert overflow.get("code") == "host_refused:capacity", overflow
    listed = await control.list_terminals()
    assert sum(row.observer_bind != "none" for row in listed) == entitlement_ceiling
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
    await direct_frames.close()
    await gclient_ws.close()
    await web_ws.close()
    await gclient_direct.close()
    await control.close()

    _restart_daemon_preserving_host(daemon_instance)
    client.close()
    client = _http(daemon_instance)
    host_after = _wait_for_host(client, daemon_instance)
    assert host_after.get("adopted") is True
    assert host_after.get("host_epoch") == epoch_before
    native_after = client.get(f"/api/terminals/{native_id}").json()
    direct_after = client.get(f"/api/terminals/{direct_id}").json()
    assert native_after.get("state") == "live"
    assert direct_after.get("state") == "live"
    wait_for_condition(
        lambda: client.get(f"/api/terminals/{exit_id}").json().get("state")
        in {"exited", "orphaned", "live"},
        timeout=20.0,
        interval=0.4,
        description="exit-during-restart reconciled",
    )

    before_host_crash: dict[str, dict[str, str]] = {}
    for run_id in run_ids:
        detail = client.get(f"/api/agents/runs/{run_id}")
        assert detail.status_code == 200, detail.text
        before_host_crash[run_id] = _stack_run_state(detail.json()["run"])
    host_pid = int(pidfile_path(socket_dir).read_text())
    os.kill(host_pid, signal.SIGKILL)
    # Both panes live on the one registered runtime, so a host crash
    # reconciles both rows; the retired tmux runtime no longer gives a second
    # pane a server to survive on.
    wait_for_condition(
        lambda: all(
            client.get(f"/api/terminals/{terminal}").json().get("state")
            in {"orphaned", "exited", "live"}
            for terminal in (native_id, direct_id)
        ),
        timeout=25.0,
        interval=0.4,
        description="both native panes reconciled after host crash",
    )

    after_cleanup: dict[str, dict[str, Any]] = {}
    try:
        for run_id in run_ids:
            run = _cancel_stack_run(client, run_id)
            if run is None:
                continue
            after_cleanup[run_id] = {
                **_stack_run_state(run),
                "cleanup_http_status": run["cleanup_http_status"],
            }
            result = str(run.get("result") or "")
            assert "GOBBY TMUX CAPTURE" in result or run.get("status") in {
                "cancelled",
                "completed",
                "failed",
                "interrupted",
            }, after_cleanup[run_id]
    except AssertionError as exc:
        evidence = write_readiness_evidence([])
        raise AssertionError(f"{exc}; cleanup evidence={evidence}") from exc
    finally:
        (tmp_path / "stack-cleanup.json").write_text(
            json.dumps({"before_host_crash": before_host_crash, "after_cleanup": after_cleanup})
        )
    client.close()


def _gclient(
    daemon: DaemonInstance, *, remote_url: str | None = None, local_url: str | None = None
) -> GclientDriver:
    env = dict(daemon.env)
    # A managed worker's credential belongs to its parent daemon, not this fixture.
    env.pop("GOBBY_AGENT_API_TOKEN", None)
    env["GOBBY_DAEMON_URL"] = local_url or daemon.http_url
    args = ["--project", str(daemon.project_dir)]
    if remote_url is not None:
        args += ["--daemon-url", remote_url]
        key_file = daemon.gobby_home / "explicit-client-api-key"
        key_file.write_text(daemon_token(daemon.gobby_home))
        key_file.chmod(0o600)
        args += ["--token-file", str(key_file)]
    return GclientDriver(args, env=env, cwd=daemon.project_dir)


@asynccontextmanager
async def _running_gclient(
    daemon: DaemonInstance, *, remote_url: str | None = None, local_url: str | None = None
) -> AsyncIterator[GclientDriver]:
    client = _gclient(daemon, remote_url=remote_url, local_url=local_url)
    try:
        yield client
    finally:
        await asyncio.to_thread(client.close)


def test_gclient_screen_tracks_fragmented_redraws_and_resize() -> None:
    screen = Screen(12, 4)
    screen.feed(b"stale pane\x1b[2")
    screen.feed(b"J\x1b[2;3Hnew\x1b[4;1Hstatus")
    assert "stale" not in screen.text
    assert screen.lines[1] == "  new       "
    assert screen.lines[3] == "status      "
    screen.feed(b"\x1b[2;3H\x1b[K")
    assert screen.lines[1] == " " * 12
    screen.resize(8, 3)
    screen.feed(b"\x1b[3;1Hresized")
    assert screen.lines == [" " * 8, " " * 8, "resized "]


def test_gclient_reaches_workspace(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        _wait_for_host(http, daemon_instance)
    with _gclient(daemon_instance) as client:
        # The sidebar bands are Machines / Projects / Agents / Terminals
        # (`SidebarSection::title`, crates/gclient/src/ui/hit.rs); an empty
        # Terminals band is hidden, so the startup sidebar shows Agents.
        client.expect("Agents")
        # Startup opens no shell of its own, so the empty workspace says so
        # (crates/gclient/tests/client_loop.rs::
        # first_run_does_not_open_a_shell_or_auto_open_roster_terminals).
        client.expect("No pane open.")
        assert client.poll() is None
        client.send("\x02")
        client.wait_for(in_prefix_mode, description="prefix mode")
        client.send("\x1b")
        client.wait_for(
            lambda screen: not in_prefix_mode(screen), description="prefix mode dismissed"
        )
        assert client.poll() is None


async def test_gclient_reorders_tabs_and_moves_a_running_pane(
    daemon_instance: DaemonInstance,
) -> None:
    async def snapshot() -> dict[str, Any]:
        session = WsSession(daemon_instance)
        await session.connect()
        try:
            await session.send(
                {
                    "type": "workspace_snapshot",
                    "request_id": "pane-move-snapshot",
                    "project_id": E2E_PROJECT_ID,
                }
            )
            return await session.wait_for(
                lambda item: item.get("type") == "workspace_snapshot"
                and item.get("request_id") == "pane-move-snapshot",
                timeout=10.0,
                description="project workspace snapshot",
            )
        finally:
            await session.close()

    async def until(predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        deadline = time.monotonic() + 15.0
        latest = await snapshot()
        while not predicate(latest) and time.monotonic() < deadline:
            await asyncio.to_thread(client.read, 0.05)
            latest = await snapshot()
        assert predicate(latest), latest
        return latest

    with _gclient(daemon_instance) as client:
        await asyncio.to_thread(client.expect, "Agents")
        # Startup opens nothing, so place one running pane to reorder and
        # move; the two chords below then bring the tab count to three.
        placed_id = await _shell(daemon_instance)
        await _adopt(daemon_instance, placed_id)
        first = await until(lambda row: len(row["tabs"]) == 1)
        original = first["panes"][0]
        terminal_id = original["terminal_id"]
        assert terminal_id == placed_id
        # chord c opens a client-local empty tab (#22883) that the daemon
        # snapshot never lists, so place the extra panes as daemon tabs where
        # the reorder and move assertions can read them.
        extra_addresses: list[str] = []
        for _ in range(2):
            extra_id = await _shell(daemon_instance)
            extra_addresses.append(await _adopt(daemon_instance, extra_id))
            await _placed_address(daemon_instance, extra_id)
        three = await until(lambda row: len(row["tabs"]) == 3)
        initial_ids = [tab["id"] for tab in three["tabs"]]
        # Activate the last tab so move_tab_left has somewhere to go.
        await _activate_terminal(client, extra_addresses[-1])

        await asyncio.to_thread(client.chord, "\x1b[1;2D")
        moved_left = await until(
            lambda row: [tab["id"] for tab in row["tabs"]]
            == [initial_ids[0], initial_ids[2], initial_ids[1]]
        )
        assert moved_left["tabs"][1]["id"] == initial_ids[2]

        await asyncio.to_thread(client.chord, "\x1b[1;2C")
        await until(lambda row: [tab["id"] for tab in row["tabs"]] == initial_ids)

        # Drag the first tab onto the last in the live tab bar. The bar
        # renders "N: title" labels below the menu row, so read its real
        # row and label columns instead of the retired "tab-0:" header.
        lines = client.screen.lines
        tab_row = next(index for index, line in enumerate(lines) if " 0: " in line)
        bar = lines[tab_row]
        source = bar.index("0: ")
        target = bar.rindex(f"{len(initial_ids) - 1}: ")
        assert target > source
        client.send(f"\x1b[<0;{source + 2};{tab_row + 1}M\x1b[<0;{target + 2};{tab_row + 1}m")
        dragged = await until(
            lambda row: [tab["id"] for tab in row["tabs"]]
            == [initial_ids[1], initial_ids[2], initial_ids[0]]
        )
        assert dragged["tabs"][2]["id"] == initial_ids[0]

        await asyncio.to_thread(client.chord, "@")
        placed = await until(
            lambda row: any(
                pane["id"] == original["id"] and pane["tab_id"] == initial_ids[2]
                for pane in row["panes"]
            )
        )
        assert len(placed["tabs"]) == 2
        assert (
            next(pane for pane in placed["panes"] if pane["id"] == original["id"])["terminal_id"]
            == terminal_id
        )

        await asyncio.to_thread(client.chord, "C")
        new_tab = await until(
            lambda row: any(
                pane["id"] == original["id"]
                and pane["tab_id"] not in {initial_ids[1], initial_ids[2]}
                for pane in row["panes"]
            )
        )
        final_tab_id = next(pane for pane in new_tab["panes"] if pane["id"] == original["id"])[
            "tab_id"
        ]
        assert any(tab["id"] == final_tab_id for tab in new_tab["tabs"])

    restored = await snapshot()
    assert any(
        pane["id"] == original["id"]
        and pane["tab_id"] == final_tab_id
        and pane["terminal_id"] == terminal_id
        for pane in restored["panes"]
    )


_SEAT_DRAFT = "[Gobby] Check messages"
# Longer than gclient's old 5 s request deadline, so held retries run past it.
_SEAT_BUSY_SECONDS = 6.0
# A Claude-framed seat holding a wake that was typed and never submitted. Like a
# CLI mid-turn, it ignores Enter until it has been busy for _SEAT_BUSY_SECONDS.
_SEAT_STUB = f"""\
import os
import sys
import time
import tty

RULE = "\\u2500" * 40
fd = sys.stdin.fileno()
tty.setraw(fd)
history = []
draft = {_SEAT_DRAFT!r}
first_input = None


def draw():
    rows = history[-12:] + [RULE, "\\u276f " + draft, RULE]
    sys.stdout.write("\\x1b[H\\x1b[2J" + "\\r\\n".join(rows))
    sys.stdout.flush()


draw()
while True:
    chunk = os.read(fd, 1024).decode(errors="replace")
    if not chunk:
        break
    now = time.monotonic()
    if first_input is None:
        first_input = now
    for char in chunk:
        if char not in "\\r\\n":
            draft += char if char.isprintable() else ""
        elif draft.strip() and now - first_input >= {_SEAT_BUSY_SECONDS!r}:
            history.append("SUBMITTED:" + draft.strip())
            draft = ""
    draw()
"""


@pytest.fixture(scope="session")
def tree_gclient() -> Path:
    """Build ``gclient`` from this tree; the installed one may predate the change."""
    repo = Path(__file__).resolve().parents[2]
    build = subprocess.run(
        ["cargo", "build", "-p", "gobby-client", "--bin", "gclient"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=2400.0,
    )
    if build.returncode != 0:
        pytest.fail(f"building gclient from the tree failed:\n{build.stdout}\n{build.stderr}")
    metadata = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=600.0,
    )
    if metadata.returncode != 0:
        pytest.fail(f"cargo metadata failed:\n{metadata.stderr}")
    binary = Path(str(json.loads(metadata.stdout)["target_directory"])) / "debug" / "gclient"
    if not binary.is_file():
        pytest.fail(f"cargo build left no gclient at {binary}")
    return binary


@pytest.mark.asyncio
async def test_gclient_send_keys_enter_submits_the_draft_a_busy_seat_held(
    daemon_instance: DaemonInstance,
    cli_events: CLIEventSimulator,
    postgres_db: Any,
    tree_gclient: Path,
    tmp_path: Path,
) -> None:
    """``send-keys --enter`` submits a seat's stuck draft and answers with the proof.

    The seat ignores Enter past gclient's old 5 s deadline, so the daemon's held
    retries outlive it; the reply waits for the read that shows the draft left
    (#23730).
    """
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
    script = tmp_path / "seat.py"
    script.write_text(_SEAT_STUB)
    created = await _ws_create(daemon_instance, [sys.executable, str(script)])
    assert created.get("success") is True, created
    terminal_id = str(created["terminal_id"])
    terminals = TerminalManager(postgres_db)
    terminal = terminals.get(terminal_id)
    assert terminal is not None and terminal.locator is not None
    host_terminal_id = str(terminal.locator["host_terminal_id"])
    seat = cli_events.register_session(
        external_id=f"send-keys-seat-{uuid.uuid4().hex}",
        source="claude",
        project_id=E2E_PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
    )["id"]
    assert terminals.bind_session(terminal_id, seat, E2E_PROJECT_ID) is not None
    pane_ref = await _adopt(daemon_instance, terminal_id)
    detector = IdleDetector(DetectionManifestRegistry(postgres_db), "claude")
    token_file = tmp_path / "gclient-token"
    token_file.write_text(daemon_token(daemon_instance.gobby_home))
    token_file.chmod(0o600)
    env = dict(daemon_instance.env)
    for name in ("GOBBY_AGENT_API_TOKEN", "GOBBY_PANE_REF", "GOBBY_WORKSPACE_ID", "GOBBY_TAB_ID"):
        env.pop(name, None)
    control = await _open_control(Path(os.environ["GOBBY_E2E_HOST_SOCKET_DIR"]))

    async def composer() -> ComposerRead:
        snapshot = await control.snapshot(
            host_terminal_id, mode="ansi", max_lines=COMPOSER_PROBE_LINES
        )
        return detector.composer_read(composer_text(str(snapshot.get("text", ""))))

    async def drafted() -> bool:
        read = await composer()
        return read.state == "draft" and read.line == _SEAT_DRAFT

    try:
        await wait_for_awaited_condition(
            drafted, timeout=10.0, interval=0.1, description="the seat's held draft"
        )
        started = time.monotonic()
        sent = await asyncio.to_thread(
            subprocess.run,
            [
                str(tree_gclient),
                "send-keys",
                "--daemon-url",
                daemon_instance.http_url,
                "--token-file",
                str(token_file),
                pane_ref,
                " ",
                "--enter",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=90.0,
        )
        elapsed = time.monotonic() - started
        assert sent.returncode == 0, sent.stderr
        assert json.loads(sent.stdout)["indeterminate"] is False
        # The seat only takes Enter once busy, so the reply cannot beat it.
        assert elapsed >= _SEAT_BUSY_SECONDS
        screen = await control.snapshot(host_terminal_id, mode="text", max_lines=40)
        assert f"SUBMITTED:{_SEAT_DRAFT}" in str(screen.get("text", ""))
        assert (await composer()).state == "empty"
    finally:
        await control.close()


class ClientWire:
    """Forward real daemon traffic, observing messages and injecting boundary faults."""

    def __init__(self, daemon: DaemonInstance) -> None:
        self.daemon = daemon
        self.sent: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.responses: dict[str, str] = {}
        self.host_version: int | None = None
        self.frame_socket: str | None = None
        self.url = ""

    async def http(self, _connection: ServerConnection, request: Request) -> Response | None:
        self.paths.append(request.path)
        if request.path == "/ws":
            return None
        authorization = request.headers.get("Authorization")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                self.daemon.http_url + request.path,
                headers={"Authorization": authorization} if authorization is not None else {},
            )
        body = response.content
        self.responses[request.path] = response.text[:3000]
        if request.path == "/api/health" and self.host_version is not None:
            payload = response.json()
            assert payload["gterm_host"]["running"] is True
            payload["gterm_host"]["protocol_version"] = self.host_version
            body = json.dumps(payload).encode()
        return Response(
            response.status_code,
            response.reason_phrase,
            Headers({"Content-Type": "application/json", "Content-Length": str(len(body))}),
            body,
        )

    async def websocket(self, downstream: ServerConnection) -> None:
        assert downstream.request is not None
        async with websockets.connect(
            self.daemon.ws_url,
            additional_headers={
                "Authorization": downstream.request.headers.get("Authorization", "")
            },
        ) as upstream:

            async def forward_client() -> None:
                async for raw in downstream:
                    self.sent.append(json.loads(raw))
                    await upstream.send(raw)

            async def forward_daemon() -> None:
                async for raw in upstream:
                    message = json.loads(raw)
                    self.received.append(message)
                    if self.frame_socket and isinstance(message.get("direct"), dict):
                        message = dict(message, direct=dict(message["direct"]))
                        message["direct"]["frame_socket_path"] = self.frame_socket
                        raw = json.dumps(message)
                    await downstream.send(raw)

            tasks = [asyncio.create_task(forward_client()), asyncio.create_task(forward_daemon())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            except websockets.ConnectionClosed:
                pass
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def running(self) -> AsyncIterator[ClientWire]:
        # Fault callbacks own their deadlines. In particular, startup outage
        # injection waits for a 20s daemon stop inside process_request; the
        # ordinary 10s WebSocket handshake budget must not cancel that wait.
        async with serve(
            self.websocket, "127.0.0.1", 0, process_request=self.http, open_timeout=None
        ) as server:
            self.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            try:
                yield self
            except Exception as exc:
                exc.add_note(f"Daemon HTTP responses: {self.responses}")
                exc.add_note(f"Client messages: {self.sent[-8:]}")
                typed = "".join(m.get("data", "") for m in self.sent)
                exc.add_note(f"Typed terminal input: {typed!r}")
                control = [
                    m
                    for m in self.received
                    if m.get("type") not in {"terminal_frame", "terminal_write_outcome"}
                    or (m.get("type") == "terminal_write_outcome" and m.get("reason"))
                ]
                exc.add_note(f"Daemon control messages: {control[-20:]}")
                raise


async def _adopt(daemon: DaemonInstance, terminal_id: str) -> str:
    """Give a terminal a workspace address and return it.

    `terminal_address` (crates/gclient/src/ui/chrome/labels.rs) leads every row
    and the status line with the pane's workspace address `n:w:t:p` when the
    workspace model places the terminal, and only falls back to the backend's
    own pane id. Nothing else tells two rows apart: the name ladder ends at the
    foreground command, so two `/bin/sh` rows read alike, and the navigator
    matches the row title and detail, which is where the address sits. So the
    tests address a terminal by adopting it into a tab of the registered
    project's default workspace, exactly where gclient attaches it.
    """
    session = WsSession(daemon)
    await session.connect()
    try:
        await session.send(
            {
                "type": "workspace_snapshot",
                "request_id": "adopt-snapshot",
                "project_id": E2E_PROJECT_ID,
            }
        )
        snapshot = await session.wait_for(
            lambda item: item.get("type") == "workspace_snapshot"
            and item.get("request_id") == "adopt-snapshot",
            timeout=10.0,
            description="workspace snapshot",
        )
        home = snapshot["workspace"]
        await session.send(
            {
                "type": "workspace_op",
                "request_id": f"adopt-{terminal_id[:8]}",
                "op": "tab.create",
                "workspace": home["id"],
                "project_id": E2E_PROJECT_ID,
                "terminal_id": terminal_id,
            }
        )
        created = await session.wait_for(
            lambda item: item.get("type") == "workspace_op"
            and item.get("request_id") == f"adopt-{terminal_id[:8]}",
            timeout=15.0,
            description=f"tab.create adopting {terminal_id}",
        )
        layout = created["result"]
        tab, pane = layout["tabs"][0], layout["panes"][0]
        return f"{home['node_ref']}:{home['ref']}:{tab['ref']}:{pane['ref']}"
    finally:
        await session.close()


async def _placed_address(
    daemon: DaemonInstance, terminal_id: str, *, timeout: float = 15.0
) -> str:
    """Wait for the address of a terminal the client placed itself.

    A terminal gclient spawns arrives in a pane of the focused tab already, so
    it has an address without being adopted and `_adopt` would refuse it a
    second pane.
    """
    session = WsSession(daemon)
    await session.connect()
    try:
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            request_id = f"placed-{attempt}-{terminal_id[:8]}"
            await session.send(
                {
                    "type": "workspace_snapshot",
                    "request_id": request_id,
                    "project_id": E2E_PROJECT_ID,
                }
            )
            snapshot = await session.wait_for(
                lambda item, wanted=request_id: item.get("type") == "workspace_snapshot"
                and item.get("request_id") == wanted,
                timeout=10.0,
                description="workspace snapshot",
            )
            home = snapshot["workspace"]
            tabs = {tab["id"]: tab for tab in snapshot["tabs"]}
            for pane in snapshot["panes"]:
                if pane["terminal_id"] == terminal_id:
                    tab = tabs[pane["tab_id"]]
                    return f"{home['node_ref']}:{home['ref']}:{tab['ref']}:{pane['ref']}"
            if time.monotonic() >= deadline:
                raise AssertionError(f"no workspace pane holds {terminal_id}")
            attempt += 1
            await asyncio.sleep(0.2)
    finally:
        await session.close()


async def _screen(client: GclientDriver, text: str, *, timeout: float = 15.0) -> None:
    await asyncio.to_thread(client.expect, text, timeout=timeout)


async def _delivery(wire: ClientWire, delivery: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Wait for a live attachment with this frame delivery, and return it.

    The status line carried the delivery until #22538 gave that segment to the
    backend enum (`render_status_line`, crates/gclient/src/ui/status.rs), so
    `direct` and `proxy` are words on the wire now rather than on the screen.
    """

    def attached() -> dict[str, Any] | None:
        return next(
            (
                item
                for item in reversed(wire.received)
                if item.get("type") == "terminal_attach_result"
                and item.get("frame_delivery") == delivery
                and item.get("success") is True
            ),
            None,
        )

    result = await asyncio.to_thread(
        wait_for_condition, attached, timeout=timeout, description=f"{delivery} attachment"
    )
    assert isinstance(result, dict)
    return result


async def _activate_terminal(client: GclientDriver, selector: str) -> None:
    await _screen(client, selector)
    await asyncio.to_thread(client.chord, "g")
    await _screen(client, "search terminals")
    client.send(selector)
    await _screen(client, f"/ {selector}")
    client.send("\r")
    await asyncio.to_thread(
        client.wait_for,
        lambda screen: "navigator" not in screen.lines[-1],
        description="terminal activation completed",
    )


async def _shell(daemon: DaemonInstance, *, marker: str = "GCLIENT-SHELL-READY") -> str:
    result = await _ws_create(
        daemon,
        ["/bin/sh", "-c", f"printf '{marker}\\n'; exec /bin/sh -i"],
    )
    assert result.get("success") is True, result
    return str(result["terminal_id"])


async def _take_and_echo(client: GclientDriver, marker: str) -> None:
    await asyncio.to_thread(client.chord, "t")
    await _screen(client, "Focused")
    # Splitting the marker means terminal echo alone cannot satisfy the assertion.
    left, right = marker.rsplit("-", 1)
    client.send(f"echo {left}-'{right}'\r")
    await _screen(client, marker)


def _stop_daemon_for_client_outage(daemon: DaemonInstance) -> None:
    """Stop only the isolated daemon, leaving its native host for adoption."""
    write_shutdown_intent("gclient-e2e-outage", ShutdownIntent.RESTART, home=daemon.gobby_home)
    os.kill(daemon.pid, signal.SIGTERM)
    wait_for_condition(
        lambda: not daemon.is_alive(),
        timeout=20.0,
        interval=0.1,
        description="isolated daemon stopped while native client remains attached",
    )


@pytest.mark.asyncio
async def test_gclient_renders_native_row_through_host(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
        terminal_id = await _shell(daemon_instance, marker="GCLIENT-ROW-OK")
        row = http.get(f"/api/terminals/{terminal_id}").json()
        assert row["backend"] == "native"
    async with ClientWire(daemon_instance).running() as wire:
        async with _running_gclient(daemon_instance, local_url=wire.url) as client:
            await _activate_terminal(client, await _adopt(daemon_instance, terminal_id))
            await _screen(client, "GCLIENT-ROW-OK")
            await _delivery(wire, "direct")
            assert client.poll() is None


@pytest.mark.asyncio
async def test_gclient_renders_native_row_direct_and_types(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
    terminal_id = await _shell(daemon_instance)
    async with ClientWire(daemon_instance).running() as wire:
        async with _running_gclient(daemon_instance, local_url=wire.url) as client:
            await _activate_terminal(client, await _adopt(daemon_instance, terminal_id))
            await _screen(client, "GCLIENT-SHELL-READY")
            await _delivery(wire, "direct")
            await _take_and_echo(client, "GCLIENT-NATIVE-OK")
            client.send(
                "printf 'GCLIENT-SLEEP-%s\\n' RUNNING; sleep 30 && echo SLEEP-'COMPLETED'\r"
            )
            await _screen(client, "GCLIENT-SLEEP-RUNNING")
            client.send(b"\x03")
            await _take_and_echo(client, "GCLIENT-INTERRUPTED-OK")
            assert "SLEEP-COMPLETED" not in client.screen.text
            inputs = [item for item in wire.sent if item.get("type") == "terminal_input"]
            # Direct delivery writes Ctrl-C on the host frame socket. The
            # completed shell marker above proves it interrupted sleep;
            # it must not be mirrored through the daemon WebSocket.
            assert not any("\x03" in item.get("data", "") for item in inputs)
            assert not any(item.get("type") == "terminal_paste" for item in wire.sent)


@pytest.mark.asyncio
async def test_gclient_survives_daemon_restart_with_usable_native_pane(
    daemon_instance: DaemonInstance,
) -> None:
    """Live #23076 2.3.1/2.3.3 proof: the real client survives a daemon restart.

    The pane's frame source hits EOF when the daemon goes down, and the client
    must reconnect straight to the still-running host, keep the same pane
    address, and keep accepting input; the daemon then adopts the same host at
    the same epoch when it comes back.
    """
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
        epoch_before = _wait_for_host(http, daemon_instance).get("host_epoch")
    assert isinstance(epoch_before, str) and epoch_before, "host epoch missing before restart"
    terminal_id = await _shell(daemon_instance, marker="GCLIENT-RESTART-BEFORE")
    async with _running_gclient(daemon_instance) as client:
        address = await _adopt(daemon_instance, terminal_id)
        await _activate_terminal(client, address)
        await _screen(client, "GCLIENT-RESTART-BEFORE")
        await _take_and_echo(client, "GCLIENT-RESTART-HELD")
        await asyncio.to_thread(_stop_daemon_for_client_outage, daemon_instance)
        try:
            await _screen(client, "Daemon unavailable")
            assert client.poll() is None, "gclient exited while the daemon was down"
            # The carried host grant must accept input before a daemon can
            # issue another lease. Split the marker to exclude terminal echo.
            client.send("echo GCLIENT-RESTART-'OFFLINE'\r")
            await _screen(client, "GCLIENT-RESTART-OFFLINE")
        finally:
            await asyncio.to_thread(daemon_instance.restart)
        assert client.poll() is None, "gclient exited when the daemon stopped"
        await _screen(client, "GCLIENT-RESTART-BEFORE")
        assert address in client.screen.text, "pane address was not retained after restart"
        await _take_and_echo(client, "GCLIENT-RESTART-AFTER")
        with _http(daemon_instance) as http:
            host_after = await asyncio.to_thread(_wait_for_host, http, daemon_instance)
            assert host_after.get("adopted") is True
            assert host_after.get("host_epoch") == epoch_before
            row = http.get(f"/api/terminals/{terminal_id}").json()
            assert row.get("state") == "live"


@pytest.mark.asyncio
async def test_gclient_survives_daemon_stop_during_startup_response(
    daemon_instance: DaemonInstance,
) -> None:
    """A daemon stop halfway through launch HTTP must keep the real client alive."""
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
    terminal_id = await _shell(daemon_instance, marker="GCLIENT-STARTUP-READY")
    address = await _adopt(daemon_instance, terminal_id)
    wire = ClientWire(daemon_instance)
    original_http = wire.http
    stopping = asyncio.Event()
    cut = asyncio.Event()

    async def truncate_after_stop(
        connection: ServerConnection, request: Request
    ) -> Response | None:
        response = await original_http(connection, request)
        if request.path != "/api/admin/config" or cut.is_set():
            return response
        assert response is not None and response.status_code == 200
        stopping.set()
        await asyncio.to_thread(_stop_daemon_for_client_outage, daemon_instance)
        cut.set()
        # Preserve the real response's successful status and Content-Length,
        # then close with an incomplete body, as an interrupted HTTP read does.
        response.body = response.body[: len(response.body) // 2]
        return response

    with patch.object(wire, "http", new=truncate_after_stop):
        async with wire.running():
            async with _running_gclient(daemon_instance, local_url=wire.url) as client:
                try:
                    # Reading the PTY also answers startup terminal queries;
                    # waiting only on the server event leaves the client parked.
                    await asyncio.to_thread(
                        client.wait_for,
                        lambda _screen: stopping.is_set(),
                        description="startup HTTP request reached outage",
                        timeout=15.0,
                    )
                    # The isolated shutdown has its own 20s contract; it must
                    # not consume the client's 15s startup-request budget.
                    await asyncio.wait_for(cut.wait(), timeout=25.0)
                    await asyncio.to_thread(daemon_instance.restart)
                    await _activate_terminal(client, address)
                    await _screen(client, "GCLIENT-STARTUP-READY")
                    assert client.poll() is None, "gclient exited on an interrupted launch response"
                    await _take_and_echo(client, "GCLIENT-STARTUP-AFTER")
                except (AssertionError, TimeoutError) as exc:
                    from tests.e2e.readiness_capture import capture_readiness_timeout

                    capture = capture_readiness_timeout(
                        daemon_instance.log_file.parent, daemon_instance.process
                    )
                    exc.add_note(f"Isolated daemon shutdown threads: {capture.thread_stack_tail}")
                    exc.add_note(f"Isolated daemon shutdown tasks: {capture.task_stack_tail}")
                    exc.add_note(f"Isolated daemon shutdown timings: {capture.startup_timing_tail}")
                    exc.add_note(
                        "Isolated daemon process state: "
                        f"pid={daemon_instance.process.pid}, "
                        f"returncode={daemon_instance.process.poll()}"
                    )
                    exc.add_note(
                        f"Isolated daemon shutdown log: {daemon_instance.read_logs()[-8000:]}"
                    )
                    exc.add_note(
                        "Isolated daemon shutdown error log: "
                        f"{daemon_instance.read_error_logs()[-8000:]}"
                    )
                    log = daemon_instance.gobby_home / "logs" / "gclient.log"
                    if log.is_file():
                        exc.add_note(
                            f"Isolated gclient exit attribution: {log.read_text()[-4000:]}"
                        )
                    raise


@pytest.mark.asyncio
async def test_gclient_remote_session_uses_proxy(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
        terminal_id = await _shell(daemon_instance)
    wire = ClientWire(daemon_instance)
    # Model a remote filesystem: the daemon's Unix socket is not reachable by this client.
    wire.frame_socket = str(daemon_instance.gobby_home / "remote-host.sock")
    async with wire.running():
        async with _running_gclient(daemon_instance, remote_url=wire.url) as client:
            await _activate_terminal(client, await _adopt(daemon_instance, terminal_id))
            await _screen(client, "GCLIENT-SHELL-READY")
            await _delivery(wire, "proxy")
            await _take_and_echo(client, "GCLIENT-PROXY-OK")
            attaches = [item for item in wire.sent if item.get("frame_delivery") == "proxy"]
            assert attaches
            assert all(item.get("encoding") == "semantic_frame" for item in attaches)
            assert any(item.get("type") == "terminal_frame" for item in wire.received)


@pytest.mark.asyncio
async def test_gclient_direct_stream_loss_recovers_to_host(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
    terminal_id = await _shell(daemon_instance)
    socket_dir = Path(os.environ["GOBBY_E2E_HOST_SOCKET_DIR"])
    tap_path = socket_dir / "tap.sock"
    writers: list[asyncio.StreamWriter] = []
    relays: set[asyncio.Task[None]] = set()

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        relays.add(task)
        writers.append(writer)
        upstream_reader, upstream_writer = await asyncio.open_unix_connection(
            str(frames_socket_path(socket_dir))
        )

        async def copy(source: asyncio.StreamReader, destination: asyncio.StreamWriter) -> None:
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()

        tasks = [
            asyncio.create_task(copy(reader, upstream_writer)),
            asyncio.create_task(copy(upstream_reader, writer)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in tasks:
                pump.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            upstream_writer.close()
            writer.close()
            await asyncio.gather(upstream_writer.wait_closed(), writer.wait_closed())
            relays.discard(task)

    wire = ClientWire(daemon_instance)
    wire.frame_socket = str(tap_path)
    server = await asyncio.start_unix_server(relay, path=str(tap_path))
    try:
        async with server, wire.running():
            async with _running_gclient(daemon_instance, local_url=wire.url) as client:
                address = await _adopt(daemon_instance, terminal_id)
                await _activate_terminal(client, address)
                await _screen(client, "GCLIENT-SHELL-READY")
                await _delivery(wire, "direct")
                old = next(
                    item["attachment_id"]
                    for item in wire.received
                    if item.get("type") == "terminal_attach_result"
                    and item.get("terminal_id") == terminal_id
                    and item.get("frame_delivery") == "direct"
                )
                # The adopted terminal is the client's only pane: startup
                # opens no shell of its own, so this tap sees one stream.
                assert len(writers) == 1
                target_writer = writers[0]
                # Cutting the direct stream is the #23076 case: the client
                # reconnects straight to the still-running host over the same
                # local socket instead of falling back to the daemon proxy.
                target_writer.close()
                await target_writer.wait_closed()
                await asyncio.to_thread(
                    wait_for_condition,
                    lambda: len(writers) == 2,
                    timeout=20.0,
                    description="gclient reconnected directly to the host",
                )
                await _take_and_echo(client, "GCLIENT-HOST-RECOVERED-OK")
                assert address in client.screen.text
                # The attachment, lease and grant survive a host reconnect, so
                # nothing finalizes and no proxy attach replaces them.
                assert not [
                    item
                    for item in wire.received
                    if item.get("type") == "terminal_attachment_finalized"
                    and item.get("attachment_id") == old
                ]
                assert not [
                    item
                    for item in wire.received
                    if item.get("type") == "terminal_attach_result"
                    and item.get("frame_delivery") == "proxy"
                ]
    finally:
        server.close()
        await server.wait_closed()
        pending = list(relays)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        tap_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_gclient_remote_refuses_absent_host(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
        socket_dir = Path(os.environ["GOBBY_E2E_HOST_SOCKET_DIR"])
        host_pid = int(pidfile_path(socket_dir).read_text())
        os.kill(host_pid, signal.SIGKILL)

        def stopped() -> dict[str, Any] | None:
            host = http.get("/api/health").json()["gterm_host"]
            return host if host.get("running") is False else None

        state = await asyncio.to_thread(
            wait_for_condition, stopped, timeout=15.0, description="isolated host reported stopped"
        )
        assert isinstance(state, dict)
    async with ClientWire(daemon_instance).running() as wire:
        async with _running_gclient(daemon_instance, remote_url=wire.url) as client:
            assert await asyncio.to_thread(client.wait_exit) == 1
            output = bytes(client.output).decode(errors="replace")
            assert "gterm host is unusable: running=false" in output
            assert "Check `gobby status`" in output
            assert "/ws" not in wire.paths
            assert b"\x1b[?1049h" not in client.output


@pytest.mark.asyncio
async def test_gclient_remote_refuses_protocol_mismatch(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        state = await asyncio.to_thread(_wait_for_host, http, daemon_instance)
    expected = int(state["protocol_version"])
    wire = ClientWire(daemon_instance)
    wire.host_version = expected + 1
    async with wire.running():
        async with _running_gclient(daemon_instance, remote_url=wire.url) as client:
            assert await asyncio.to_thread(client.wait_exit) == 1
            output = bytes(client.output).decode(errors="replace")
            assert "gterm host is unusable: running=true" in output
            assert f"protocol_version={expected + 1}" in output
            assert f"expected_protocol_version={expected}" in output
            assert "Check `gobby status`" in output
            assert "/ws" not in wire.paths
            assert b"\x1b[?1049h" not in client.output


@pytest.mark.asyncio
async def test_gclient_spawns_and_terminates_a_terminal(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
        survivor_id = await _shell(daemon_instance, marker="GCLIENT-SURVIVOR-READY")
        client_dir = daemon_instance.gobby_home / "client"
        client_dir.mkdir(parents=True, exist_ok=True)
        (client_dir / "keymap.toml").write_text(
            '[bindings]\nnew_terminal = "prefix+i"\n', encoding="utf-8"
        )
        # confirm_close defaults on, which turns chord D into the "Close
        # terminal?" dialog instead of a close. Pin it off so the node
        # exercises the actual terminate path.
        (client_dir / "prefs.toml").write_text("[ui]\nconfirm_close = false\n", encoding="utf-8")
        async with _running_gclient(daemon_instance) as client:
            survivor = await _adopt(daemon_instance, survivor_id)
            await _activate_terminal(client, survivor)
            await _screen(client, "GCLIENT-SURVIVOR-READY")
            # Startup opens no shell of its own, so the survivor is the only
            # row before the chord and anything new is the one it spawned.
            existing_ids = {row["id"] for row in _list_items(http)}
            assert existing_ids == {survivor_id}, existing_ids
            await asyncio.to_thread(client.chord, "i")

            def spawned() -> dict[str, Any] | None:
                return next(
                    (row for row in _list_items(http) if row["id"] not in existing_ids), None
                )

            row = await asyncio.to_thread(
                wait_for_condition,
                spawned,
                timeout=15.0,
                description="gclient spawned terminal row",
            )
            assert isinstance(row, dict)
            spawned_id = row["id"]
            spawned_address = await _placed_address(daemon_instance, spawned_id)
            await _screen(client, spawned_address)
            await asyncio.to_thread(client.chord, "l")

            def spawned_focused(screen: Screen) -> bool:
                # The focused badge renders on the focused pane's top border
                # (render_pane_border_titles, crates/gclient/src/ui/panes.rs);
                # the final row is the global prefix hint, not pane metadata.
                return any(
                    "Focused" in line and line.rstrip().endswith("┐") for line in screen.lines
                )

            await asyncio.to_thread(
                client.wait_for,
                spawned_focused,
                description="spawned terminal selected",
            )
            await _take_and_echo(client, "GCLIENT-SPAWNED-OK")
            await asyncio.to_thread(client.chord, "D")
            await asyncio.to_thread(
                wait_for_condition,
                lambda: all(row["id"] != spawned_id for row in _list_items(http)),
                timeout=15.0,
                description="terminated row removed from daemon listing",
            )
            await asyncio.to_thread(
                client.wait_for,
                lambda screen: spawned_address not in screen.text and survivor in screen.text,
                description="terminated pane removed and survivor retained",
            )
            await _take_and_echo(client, "GCLIENT-SURVIVOR-STILL-LIVE")


@pytest.mark.asyncio
async def test_gclient_follows_a_live_pty_resize(daemon_instance: DaemonInstance) -> None:
    with _http(daemon_instance) as http:
        await asyncio.to_thread(_wait_for_host, http, daemon_instance)
    terminal_id = await _shell(daemon_instance, marker="GCLIENT-BEFORE-RESIZE")
    async with _running_gclient(daemon_instance) as client:
        address = await _adopt(daemon_instance, terminal_id)
        await _activate_terminal(client, address)
        await _screen(client, "GCLIENT-BEFORE-RESIZE")
        assert client.screen.cols == 120
        assert client.screen.rows == 40
        # The focused badge sits on the pane's top border and the address on
        # its bottom one; the last row is the global status line, which names
        # only the prefix cue (`render_pane_border_titles` / `render_status_line`,
        # crates/gclient/src/ui/panes.rs and status.rs).
        assert any(
            "Focused" in line and line.rstrip().endswith("┐") for line in client.screen.lines
        )
        client.resize(100, 32)
        await _screen(client, "GCLIENT-BEFORE-RESIZE")
        # A resize repaints the whole frame: wait for the redrawn pane borders
        # and the status line on the new bottom row, not a mid-redraw frame.
        await asyncio.to_thread(
            client.wait_for,
            lambda screen: len(screen.lines) == 32
            and screen.lines[-1].rstrip().endswith("prefix ctrl+b")
            and any("Focused" in line and line.rstrip().endswith("┐") for line in screen.lines)
            and any(line.rstrip().endswith("┘") for line in screen.lines),
            description="focused pane frame closed at the resized width",
        )
        assert len(client.screen.lines) == 32
        assert all(len(line) == 100 for line in client.screen.lines)
        await _take_and_echo(client, "GCLIENT-AFTER-RESIZE")
