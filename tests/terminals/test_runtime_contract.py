"""Cross-backend TerminalRuntime contract suite (plan 5.1)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from gobby.agents.spawn_executor import derive_spawn_key
from gobby.agents.tmux.output_reader import TmuxOutputReader
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.agents.tmux.text_injection import TmuxTextInjectionError
from gobby.config.terminal_host import TerminalHostConfig
from gobby.config.terminals import TerminalConfig
from gobby.config.tmux import TmuxConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.terminals import Terminal
from gobby.terminals.frame_client import FrameClient
from gobby.terminals.host_client import HostCommandError
from gobby.terminals.host_manager import TerminalHostManager
from gobby.terminals.host_protocol import frames_socket_path
from gobby.terminals.native_runtime import HostManagerControl, NativeTerminalRuntime
from gobby.terminals.runtime import (
    Delivered,
    TerminalRuntime,
    TerminalSpawnRequest,
    TerminalWriteError,
)
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from tests._timing import wait_for_condition
from tests.e2e.conftest import (
    DaemonInstance,
    _postgres_url_for_schema,
    _seed_e2e_runtime_state,
    daemon_token,
    find_free_port,
    prepare_daemon_env,
    terminate_process_tree,
    wait_for_daemon_health,
    wait_for_port,
)
from tests.terminals.conftest import gterm_binary, require_backend

pytestmark = pytest.mark.integration

E2E_PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"
SPAWN_ROWS, SPAWN_COLS = 24, 80
READY = "CONTRACT-READY"
MARKER = "CONTRACT-MARKER-9f3c"
RENDERED = "盒🙂"
_PROBE = f"""\
import os
import select
import sys
import termios

fd = sys.stdin.fileno()
attrs = termios.tcgetattr(fd)
attrs[3] &= ~termios.ECHO
termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
sys.stdout.write({READY!r} + "\\n")
sys.stdout.flush()
while True:
    line = sys.stdin.readline()
    if not line:
        break
    cmd = line.strip()
    if cmd == "COLUMNS":
        size = os.get_terminal_size()
        sys.stdout.write(f"COLUMNS={{size.columns}}x{{size.lines}}\\n")
        sys.stdout.flush()
    elif cmd.startswith("ECHO "):
        sys.stdout.write(cmd[5:] + "\\n")
        sys.stdout.flush()
    elif cmd == "RENDER":
        sys.stdout.write("\\x1b[31mRED\\x1b[0m {RENDERED}\\n")
        sys.stdout.flush()
    elif cmd == "FILL":
        sys.stdout.write(("FILL-LINE\\n") * 80)
        sys.stdout.flush()
    elif cmd == "QUERY":
        sys.stdout.write("\\x1b[6n")
        sys.stdout.flush()
        ready, _, _ = select.select([sys.stdin], [], [], 0.4)
        extra = sys.stdin.read(64) if ready else ""
        sys.stdout.write(f"CPR_LEN={{len(extra)}}\\n")
        sys.stdout.flush()
    elif cmd == "EXIT":
        raise SystemExit(0)
"""


def _short_dir(prefix: str) -> Path:
    path = Path(f"/tmp/{prefix}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _terminal_from_prepared(
    *,
    backend: str,
    terminal_id: UUID,
    spawn_key: str,
    prepared: Any,
    rows: int,
    cols: int,
) -> Terminal:
    now = datetime.now(UTC)
    stored = dict(prepared.stored_locator or {})
    epoch = ""
    if prepared.locator is not None:
        epoch = str(prepared.locator.frame_host_epoch or "")
    return Terminal(
        id=str(terminal_id),
        backend=backend,
        ownership="gobby",
        state="live",
        machine_id=str(uuid4()),
        project_id=str(uuid4()),
        created_at=now,
        updated_at=now,
        attempt_generation=1,
        attempt_started_at=now,
        unresolved_writes={},
        spawn_key=spawn_key,
        locator=stored,
        locator_key=prepared.locator_key,
        session_name=spawn_key if backend == "tmux" else None,
        host_epoch=epoch or None,
        rows=rows,
        cols=cols,
    )


async def _wait_async[T](
    predicate: Callable[[], Awaitable[T]],
    *,
    timeout: float,
    description: str,
    interval: float = 0.05,
) -> T:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if loop.time() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        await asyncio.sleep(min(interval, max(deadline - loop.time(), 0)))


async def _wait_snapshot(
    runtime: TerminalRuntime,
    terminal: Terminal,
    needle: str,
    *,
    description: str,
) -> str:
    async def ready() -> str:
        snap = await runtime.snapshot(terminal, lines=80)
        return snap.text if needle in snap.text else ""

    return await _wait_async(ready, timeout=8.0, description=description)


@dataclass
class ContractHarness:
    """Live runtime plus the backend process it owns for one contract cell."""

    backend: str
    runtime: TerminalRuntime
    terminal: Terminal
    workdir: Path
    host: TerminalHostManager | None = None
    sessions: TmuxSessionManager | None = None
    tmux_socket: Path | None = None
    frame_client: FrameClient | None = None
    outputs: list[str] = field(default_factory=list)
    _fifo: TmuxOutputReader | None = None

    async def close(self) -> None:
        try:
            if self._fifo is not None:
                await self._fifo.stop_reader("contract")
        except Exception:
            pass
        try:
            await self.runtime.terminate(self.terminal, 0.2)
        except Exception:
            pass
        if self.frame_client is not None:
            await self.frame_client.close()
        if self.host is not None:
            await self.host.stop(preserve_host=False)
        if self.sessions is not None and self.tmux_socket is not None:
            await self.sessions._run("kill-server")
        shutil.rmtree(self.workdir, ignore_errors=True)
        if self.host is not None:
            shutil.rmtree(self.host.socket_dir, ignore_errors=True)
        if self.tmux_socket is not None:
            self.tmux_socket.unlink(missing_ok=True)


async def _open_frame_client(
    socket_dir: Path,
    epoch: str,
    *,
    host_terminal_id: str | None = None,
    cols: int = SPAWN_COLS,
    rows: int = SPAWN_ROWS,
) -> FrameClient:
    from gobby.storage.terminals import AttachLocator

    token_path = socket_dir / "local_cli_token"
    token = token_path.read_text(encoding="utf-8").strip() if token_path.is_file() else ""
    reader, writer = await asyncio.open_unix_connection(str(frames_socket_path(socket_dir)))
    client = FrameClient(reader, writer)
    await client.handshake(
        AttachLocator(backend="native", frame_host_epoch=epoch, host_terminal_id=host_terminal_id),
        local_token=token,
        cols=cols,
        rows=rows,
    )
    return client


async def _start_harness(backend: str) -> ContractHarness:
    workdir = _short_dir("gobby-rt")
    script = workdir / "probe.py"
    script.write_text(_PROBE)
    command = [sys.executable, "-u", str(script)]
    terminal_id = uuid4()
    spawn_key = derive_spawn_key(backend, str(terminal_id))
    request = TerminalSpawnRequest(
        terminal_id=terminal_id,
        spawn_key=spawn_key,
        command=command,
        cwd=str(workdir),
        rows=SPAWN_ROWS,
        cols=SPAWN_COLS,
    )
    host: TerminalHostManager | None = None
    sessions: TmuxSessionManager | None = None
    tmux_socket: Path | None = None
    frame: FrameClient | None = None
    if backend == "tmux":
        tmux_socket = Path(f"/tmp/gobby-rt-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock")
        sessions = TmuxSessionManager(TmuxConfig(socket_name="", socket_path=str(tmux_socket)))
        runtime: TerminalRuntime = TmuxTerminalRuntime(sessions)
        prepared = await runtime.prepare_spawn(request)
        prepared.acknowledge_persist()
        await runtime.commit_spawn(prepared)
        terminal = _terminal_from_prepared(
            backend=backend,
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            prepared=prepared,
            rows=SPAWN_ROWS,
            cols=SPAWN_COLS,
        )
        fifo = TmuxOutputReader(TmuxConfig(socket_name="", socket_path=str(tmux_socket)))
        outputs: list[str] = []

        async def on_output(_run_id: str, data: str) -> None:
            outputs.append(data)

        fifo.set_output_callback(on_output)
        await fifo.start_reader("contract", spawn_key)
        harness = ContractHarness(
            backend=backend,
            runtime=runtime,
            terminal=terminal,
            workdir=workdir,
            sessions=sessions,
            tmux_socket=tmux_socket,
            outputs=outputs,
            _fifo=fifo,
        )
    else:
        binary = gterm_binary()
        assert binary is not None
        socket_dir = _short_dir("gobby-host")
        (socket_dir / "local_cli_token").write_text("contract-frame-token\n", encoding="utf-8")
        host = TerminalHostManager(
            config=TerminalHostConfig(
                enabled=True,
                socket_dir=str(socket_dir),
                binary_path=str(binary),
                health_interval_seconds=3600.0,
            ),
            terminal_config=TerminalConfig(),
        )
        await host.start()
        if not host.native_available or host.host_epoch is None:
            await host.stop(preserve_host=False)
            shutil.rmtree(socket_dir, ignore_errors=True)
            pytest.fail(f"gterm host failed to start: {host.last_error}")
        epoch = str(host.host_epoch)
        runtime = NativeTerminalRuntime(
            HostManagerControl(host),
            frame_host_epoch=epoch,
        )
        runtime._subscribed = True
        reservation = await runtime.reserve_observer(terminal_id)
        request.reservation_id = reservation["reservation_id"]
        request.reserve_key = reservation["reserve_key"]
        prepared = await runtime.prepare_spawn(request)
        prepared.acknowledge_persist()
        await runtime.bind_observer(prepared, reservation["reservation_id"])
        await runtime.commit_spawn(prepared)
        terminal = _terminal_from_prepared(
            backend=backend,
            terminal_id=terminal_id,
            spawn_key=spawn_key,
            prepared=prepared,
            rows=SPAWN_ROWS,
            cols=SPAWN_COLS,
        )
        frame = runtime._frame_client if isinstance(runtime._frame_client, FrameClient) else None
        harness = ContractHarness(
            backend=backend,
            runtime=runtime,
            terminal=terminal,
            workdir=workdir,
            host=host,
            frame_client=frame,
        )
    await _wait_snapshot(harness.runtime, harness.terminal, READY, description="prompt-ready")
    return harness


def _frame_text(message: dict[str, Any]) -> str:
    if message.get("type") != "frame":
        return ""
    width = int(message.get("width") or 0)
    cells = message.get("cells")
    if not isinstance(cells, list) or width <= 0:
        return str(message.get("text") or "")
    rows: list[str] = []
    current: list[str] = []
    for index, cell in enumerate(cells):
        if index and index % width == 0:
            rows.append("".join(current))
            current = []
        current.append(str(cell.get("symbol") or "") if isinstance(cell, dict) else "")
    if current:
        rows.append("".join(current))
    return "\n".join(rows)


async def _collect_native_output(client: FrameClient, needle: str) -> str:
    gathered = ""

    async def pending() -> str:
        nonlocal gathered
        try:
            message = await asyncio.wait_for(client.read_message(), timeout=0.2)
        except TimeoutError:
            return gathered if needle in gathered else ""
        gathered += _frame_text(message)
        return gathered if needle in gathered else ""

    try:
        return await _wait_async(
            pending, timeout=6.0, interval=0.01, description=f"native frame {needle}"
        )
    except AssertionError:
        return gathered


@pytest.mark.asyncio
async def test_contract_matrix(contract_backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = await _start_harness(contract_backend)
    runtime = harness.runtime
    terminal = harness.terminal
    try:
        assert runtime.backend == contract_backend
        delivered = await runtime.write_text(terminal, f"ECHO {MARKER}", submit=True)
        assert isinstance(delivered, Delivered)
        text = await _wait_snapshot(runtime, terminal, MARKER, description="literal echo")
        assert MARKER in text
        if contract_backend == "tmux":
            await _assert_tmux_events(harness)
        else:
            await _assert_native_events(harness)
        key = await runtime.write_key(terminal, "enter")
        assert isinstance(key, Delivered)
        render = await runtime.write_text(terminal, "RENDER", submit=True)
        assert isinstance(render, Delivered)
        rendered = await _wait_snapshot(runtime, terminal, RENDERED, description="unicode render")
        assert RENDERED in rendered
        assert "RED" in rendered
        await runtime.resize(terminal, 30, 100)
        cols = await runtime.write_text(terminal, "COLUMNS", submit=True)
        assert isinstance(cols, Delivered)

        async def resized() -> str:
            snap = await runtime.snapshot(terminal, lines=80)
            return snap.text if "COLUMNS=100x30" in snap.text else ""

        await _wait_async(resized, timeout=6.0, description="$COLUMNS")
        fill = await runtime.write_text(terminal, "FILL", submit=True)
        assert isinstance(fill, Delivered)
        await _wait_snapshot(runtime, terminal, "FILL-LINE", description="fill history")
        visible = await runtime.snapshot(terminal, lines=8)
        full = await runtime.snapshot_full(terminal)
        assert visible.total_bytes is not None
        assert full.total_bytes is None or full.total_bytes >= visible.total_bytes
        assert "FILL-LINE" in full.text
        if contract_backend == "tmux":
            await _assert_tmux_partial(runtime, terminal, monkeypatch)
        else:
            assert isinstance(runtime, NativeTerminalRuntime)
            await _assert_native_viewers(harness)
            await _assert_native_partial(runtime, terminal, monkeypatch)
        locator = await runtime.attach_locator(terminal)
        assert locator.backend == contract_backend
        if contract_backend == "native":
            assert locator.host_terminal_id
            if harness.frame_client is not None:
                await harness.frame_client.detach()
                await harness.frame_client.attach_terminal(locator)
        still = await runtime.snapshot(terminal, lines=20)
        assert READY in still.text or MARKER in still.text or "FILL-LINE" in still.text
        assert await runtime.is_live(terminal) is True
        await runtime.write_key(terminal, "enter")
        await runtime.write_text(terminal, "EXIT", submit=True)

        async def dead() -> str:
            if not await runtime.is_live(terminal):
                return "gone"
            if contract_backend != "native":
                return ""
            before = (await runtime.snapshot(terminal, lines=30)).text
            try:
                await runtime.write_text(terminal, "ECHO after-exit", submit=True)
            except TerminalWriteError:
                return "gone"
            after = (await runtime.snapshot(terminal, lines=30)).text
            return "gone" if "after-exit" not in after or after == before else ""

        await _wait_async(dead, timeout=6.0, description="clean exit")
        if not await runtime.is_live(terminal):
            with pytest.raises(TerminalWriteError) as exc:
                await runtime.write_text(terminal, "ECHO gone", submit=True)
            assert exc.value.stage in {"none", "partial"}
        await _assert_terminate_with_grace(contract_backend)
    finally:
        await harness.close()


async def _assert_tmux_events(harness: ContractHarness) -> None:
    async def arrived() -> list[str]:
        return harness.outputs if any(MARKER in chunk for chunk in harness.outputs) else []

    chunks = await _wait_async(arrived, timeout=6.0, description="tmux FIFO terminal_output")
    assert "".join(chunks).count(MARKER) >= 1


async def _assert_native_events(harness: ContractHarness) -> None:
    client = harness.frame_client
    if client is None:
        pytest.fail("native observer frame client missing")
    gathered = await _collect_native_output(client, MARKER)
    if MARKER not in gathered:
        snap = await harness.runtime.snapshot(harness.terminal, lines=80)
        assert MARKER in snap.text
    else:
        assert MARKER in gathered


async def _assert_native_viewers(harness: ContractHarness) -> None:
    host = harness.host
    assert host is not None and host.host_epoch is not None
    locator = await harness.runtime.attach_locator(harness.terminal)
    viewer = await _open_frame_client(
        host.socket_dir,
        str(host.host_epoch),
        host_terminal_id=locator.host_terminal_id,
        cols=40,
        rows=12,
    )
    try:
        await viewer.attach_terminal(locator)
        await viewer.set_viewport(12, 40)
        await harness.runtime.write_text(harness.terminal, "COLUMNS", submit=True)

        async def still_authoritative() -> str:
            snap = await harness.runtime.snapshot(harness.terminal, lines=80)
            return snap.text if "COLUMNS=100x30" in snap.text else ""

        await _wait_async(
            still_authoritative,
            timeout=6.0,
            description="native authoritative size",
        )
        await harness.runtime.write_text(harness.terminal, "QUERY", submit=True)
        queried = await _wait_snapshot(
            harness.runtime, harness.terminal, "CPR_LEN=", description="single CPR"
        )
        assert "CPR_LEN=" in queried
    finally:
        await viewer.close()


async def _assert_tmux_partial(
    runtime: TerminalRuntime,
    terminal: Terminal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> None:
        raise TmuxTextInjectionError(
            "enter withheld",
            command=("tmux", "send-keys"),
            stderr="injected",
            returncode=1,
        )

    import gobby.terminals.tmux_runtime as tmux_runtime

    with monkeypatch.context() as patch:
        patch.setattr(tmux_runtime, "send_enter_key_to_tmux_target", boom)
        with pytest.raises(TerminalWriteError) as exc:
            await runtime.write_text(terminal, "ECHO partial", submit=True)
    assert exc.value.stage == "partial"


async def _assert_native_partial(
    runtime: NativeTerminalRuntime,
    terminal: Terminal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = runtime._client
    real_write = control.write
    writes = {"count": 0}

    async def write(**kwargs: Any) -> dict[str, Any]:
        writes["count"] += 1
        if writes["count"] >= 2:
            raise HostCommandError("injected")
        payload = await real_write(**kwargs)
        return cast(dict[str, Any], payload)

    with monkeypatch.context() as patch:
        patch.setattr(control, "write", write, raising=False)
        with pytest.raises(TerminalWriteError) as exc:
            await runtime.write_text(terminal, "ECHO partial", submit=True)
    assert exc.value.stage == "partial"


async def _assert_terminate_with_grace(backend: str) -> None:
    hang = await _start_harness(backend)
    try:
        await hang.runtime.terminate(hang.terminal, 0.4)

        async def gone() -> str:
            return "gone" if not await hang.runtime.is_live(hang.terminal) else ""

        await _wait_async(gone, timeout=6.0, description="terminate-with-grace")
    finally:
        await hang.close()


def _patch_daemon_backend(
    postgres_db: HubDatabase,
    *,
    backend: str,
    socket_dir: Path,
    binary: Path | None,
) -> None:
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch

    values: dict[str, object] = {
        "terminals.default_backend": backend,
        "terminal_host.socket_dir": str(socket_dir),
        "terminal_host.enabled": True,
    }
    if binary is not None:
        values["terminal_host.binary_path"] = str(binary)
    mutations = ConfigMutations(postgres_db)
    mutations.patch_internal(
        expected_revision=mutations.repository.current_revision(),
        patch=ConfigPatch(values=values),
        source="runtime-contract",
    )


def _start_isolated_daemon(
    *,
    postgres_db: HubDatabase,
    postgres_database_url: str,
    postgres_schema: str,
    backend: str,
) -> DaemonInstance:
    home = _short_dir("gobby-rt-home")
    socket_dir = _short_dir("gobby-rt-host")
    binary = gterm_binary()
    _seed_e2e_runtime_state(postgres_db, home)
    _patch_daemon_backend(postgres_db, backend=backend, socket_dir=socket_dir, binary=binary)
    (home / "machine_id").write_text(MACHINE_ID)
    http_port = find_free_port()
    ws_port = find_free_port()
    log_dir = home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.yaml"
    config_path.write_text("test_mode: true\n")
    postgres_url = _postgres_url_for_schema(postgres_database_url, postgres_schema)
    (home / "bootstrap.yaml").write_text(
        "\n".join(
            [
                "hub_backend: postgres",
                f"database_url: {postgres_url}",
                f"daemon_port: {http_port}",
                "bind_host: localhost",
                f"websocket_port: {ws_port}",
                f"files_home: {home / 'files'}",
            ]
        )
        + "\n"
    )
    (home / "bootstrap.yaml").chmod(0o600)
    (home / "files").mkdir(exist_ok=True)
    env = prepare_daemon_env(home_dir=home)
    env["GOBBY_CONFIG"] = str(config_path)
    env["GOBBY_HOME"] = str(home)
    native_dir = gterm_binary()
    if native_dir is not None:
        env["GOBBY_NATIVE_BIN_DIR"] = str(native_dir.parent)
    command = [sys.executable, "-m", "gobby.runner", "--config", str(config_path)]
    log_file = log_dir / "daemon.log"
    error_log_file = log_dir / "daemon_error.log"
    with log_file.open("w") as log_f, error_log_file.open("w") as err_f:
        process = __import__("subprocess").Popen(
            command,
            stdout=log_f,
            stderr=err_f,
            stdin=__import__("subprocess").DEVNULL,
            cwd=str(home),
            env=env,
            start_new_session=True,
        )
    instance = DaemonInstance(
        process=process,
        pid=process.pid,
        http_port=http_port,
        ws_port=ws_port,
        project_dir=home,
        gobby_dir=home / ".gobby",
        log_file=log_file,
        error_log_file=error_log_file,
        db_path=home / "hub-postgres.db",
        config_path=config_path,
        command=command,
        env=env,
    )
    if not wait_for_daemon_health(http_port, timeout=30.0):
        terminate_process_tree(process.pid)
        pytest.fail(
            "contract daemon failed to start\n"
            f"{instance.read_logs()[-2000:]}\n{instance.read_error_logs()[-2000:]}"
        )
    if not wait_for_port(ws_port, timeout=10.0):
        terminate_process_tree(process.pid)
        pytest.fail("contract daemon websocket was not ready")
    return instance


async def _recv_until(
    websocket: Any,
    predicate: Callable[[dict[str, Any]], str],
    *,
    timeout: float,
    description: str,
) -> str:
    async def pending() -> str:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=min(2.0, timeout))
        except TimeoutError:
            return ""
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return ""
        return predicate(payload)

    return await _wait_async(pending, timeout=timeout, interval=0.01, description=description)


async def _ws_create(daemon: DaemonInstance, command: list[str]) -> str:
    import websockets

    token = daemon_token(daemon.gobby_home)
    async with websockets.connect(
        daemon.ws_url,
        additional_headers=[("Authorization", f"Bearer {token}")],
        open_timeout=8.0,
        close_timeout=2.0,
    ) as websocket:
        welcome = await asyncio.wait_for(websocket.recv(), timeout=8.0)
        assert isinstance(welcome, str)
        await websocket.send(
            json.dumps(
                {
                    "type": "terminal_create",
                    "request_id": "contract-create",
                    "rows": SPAWN_ROWS,
                    "cols": SPAWN_COLS,
                    "cwd": str(daemon.project_dir),
                    "command": command,
                    "project_id": E2E_PROJECT_ID,
                }
            )
        )

        def created(payload: dict[str, Any]) -> str:
            if payload.get("type") == "terminal_create_result" and payload.get("success"):
                terminal_id = payload.get("terminal_id")
                return str(terminal_id) if isinstance(terminal_id, str) else ""
            return ""

        terminal_id = await _recv_until(
            websocket, created, timeout=20.0, description="terminal_create"
        )
        await websocket.send(
            json.dumps(
                {
                    "type": "terminal_attach",
                    "request_id": "contract-attach",
                    "terminal_id": terminal_id,
                    "frame_delivery": "proxy",
                }
            )
        )

        def streamed(payload: dict[str, Any]) -> str:
            if payload.get("type") in {
                "terminal_attach_result",
                "terminal_output",
                "terminal_attach_history",
            }:
                return "ok"
            return ""

        try:
            await _recv_until(websocket, streamed, timeout=12.0, description="create stream")
        except (AssertionError, TimeoutError):
            pass
        return terminal_id


def _restart_daemon_preserving_host(daemon: DaemonInstance) -> None:
    from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent

    write_shutdown_intent("runtime-contract", ShutdownIntent.RESTART, home=daemon.gobby_home)
    os.kill(daemon.pid, signal.SIGTERM)
    wait_for_condition(
        lambda: not daemon.is_alive(),
        timeout=20.0,
        interval=0.1,
        description="daemon pid exit for restart",
    )
    daemon.restart()


def _health(client: httpx.Client) -> dict[str, Any]:
    response = client.get("/api/health")
    response.raise_for_status()
    payload = response.json()
    host = payload.get("gterm_host")
    return cast(dict[str, Any], host) if isinstance(host, dict) else {}


def _list_live(client: httpx.Client, terminal_id: str) -> dict[str, Any]:
    response = client.get("/api/terminals", params={"project_id": E2E_PROJECT_ID})
    response.raise_for_status()
    body = response.json()
    for item in body.get("items") or []:
        if item.get("terminal_id") == terminal_id or item.get("id") == terminal_id:
            return cast(dict[str, Any], item)
    raise AssertionError(f"terminal {terminal_id} missing from {body}")


@pytest.mark.asyncio
async def test_daemon_restart_continuity(
    contract_backend: str,
    postgres_db: HubDatabase,
    postgres_database_url: str,
    postgres_schema: str,
) -> None:
    require_backend(contract_backend)
    daemon = _start_isolated_daemon(
        postgres_db=postgres_db,
        postgres_database_url=postgres_database_url,
        postgres_schema=postgres_schema,
        backend=contract_backend,
    )
    script = daemon.project_dir / "probe.py"
    script.write_text(_PROBE)
    command = [sys.executable, "-u", str(script)]
    client = httpx.Client(
        base_url=daemon.http_url,
        headers={"Authorization": f"Bearer {daemon_token(daemon.gobby_home)}"},
        timeout=10.0,
    )
    try:
        if contract_backend == "native":

            def host_up() -> dict[str, Any]:
                host = _health(client)
                return host if host.get("running") and host.get("host_epoch") else {}

            wait_for_condition(host_up, timeout=20.0, interval=0.2, description="gterm host")
        terminal_id = await _ws_create(daemon, command)
        before = _list_live(client, terminal_id)
        assert before.get("state") == "live"
        host_before = _health(client)
        epoch_before = host_before.get("host_epoch")
        _restart_daemon_preserving_host(daemon)
        client.close()
        client = httpx.Client(
            base_url=daemon.http_url,
            headers={"Authorization": f"Bearer {daemon_token(daemon.gobby_home)}"},
            timeout=10.0,
        )
        after = _list_live(client, terminal_id)
        assert after.get("state") == "live"
        assert after.get("terminal_id", after.get("id")) == terminal_id
        host_after = _health(client)
        if contract_backend == "native":
            assert host_after.get("adopted") is True
            assert host_after.get("host_epoch") == epoch_before
            assert host_after.get("orphaned_terminals", 0) == 0
        resumed = await _ws_create_stream_resume(daemon, terminal_id)
        assert resumed is True
    finally:
        client.close()
        if daemon.is_alive():
            daemon.stop()
        shutil.rmtree(daemon.project_dir, ignore_errors=True)


async def _ws_create_stream_resume(daemon: DaemonInstance, terminal_id: str) -> bool:
    import websockets

    token = daemon_token(daemon.gobby_home)
    async with websockets.connect(
        daemon.ws_url,
        additional_headers=[("Authorization", f"Bearer {token}")],
        open_timeout=8.0,
        close_timeout=2.0,
    ) as websocket:
        await asyncio.wait_for(websocket.recv(), timeout=8.0)
        await websocket.send(
            json.dumps(
                {
                    "type": "terminal_attach",
                    "request_id": "contract-resume",
                    "terminal_id": terminal_id,
                    "frame_delivery": "proxy",
                }
            )
        )

        def attached(payload: dict[str, Any]) -> str:
            if payload.get("type") in {
                "terminal_attach_result",
                "terminal_output",
                "terminal_attach_history",
            }:
                return "ok"
            return ""

        await _recv_until(websocket, attached, timeout=12.0, description="stream resume")
        return True
