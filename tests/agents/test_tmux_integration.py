"""Integration tests for real tmux session semantics."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from gobby.agents.tmux.output_reader import TmuxOutputReader
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.tmux import TmuxConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.runtime import TerminalSpawnRequest
from gobby.terminals.services import TerminalServices
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from gobby.utils.machine_id import require_machine_id
from tests.agents.cleanup_test_support import _handler, _stub_runtime_cleanup
from tests.servers.test_tmux_mixin import MockWebSocket

pytestmark = pytest.mark.integration


@pytest.fixture
def tmux_socket_name() -> Iterator[str]:
    """Provide a unique tmux socket name and remove its server at teardown."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")

    socket_name = f"gobby-test-{uuid4().hex}"
    yield socket_name
    subprocess.run(
        ["tmux", "-L", socket_name, "-f", "/dev/null", "kill-server"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


@pytest.fixture
def tmux_config(tmux_socket_name: str) -> TmuxConfig:
    return TmuxConfig(socket_name=tmux_socket_name, config_file="/dev/null")


@pytest.fixture
async def tmux_manager(tmux_config: TmuxConfig) -> AsyncIterator[TmuxSessionManager]:
    manager = TmuxSessionManager(tmux_config)
    yield manager
    await manager.shutdown()


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not met before timeout")


def _path_contains(path: Path, expected: str) -> bool:
    return path.exists() and expected in path.read_text(encoding="utf-8")


async def test_get_session_requires_exact_session_name(
    tmux_manager: TmuxSessionManager,
) -> None:
    await tmux_manager.create_session(
        name="agent-extra",
        command="tail -f /dev/null",
    )

    assert await tmux_manager.get_session("agent") is None

    exact = await tmux_manager.get_session("agent-extra")
    assert exact is not None
    assert exact.name == "agent-extra"


async def test_kill_session_requires_exact_session_name(
    tmux_manager: TmuxSessionManager,
) -> None:
    await tmux_manager.create_session(
        name="agent-157",
        command="tail -f /dev/null",
    )

    assert await tmux_manager.kill_session("agent-15", missing_ok=True, timeout=0.1) is True

    exact = await tmux_manager.get_session("agent-157")
    assert exact is not None
    assert exact.name == "agent-157"


async def test_create_session_preserves_env_value_with_trailing_semicolon(
    tmux_manager: TmuxSessionManager,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "env-value.txt"

    await tmux_manager.create_session(
        name="env-semicolon",
        command='printf "%s" "$TRAILING_VALUE" > "$GOBBY_OUT"; tail -f /dev/null',
        env={
            "GOBBY_OUT": str(output_path),
            "TRAILING_VALUE": "value;",
        },
    )

    await _wait_for(lambda: output_path.exists())
    assert output_path.read_text(encoding="utf-8") == "value;"


async def test_send_keys_pastes_multiline_literal_text(
    tmux_manager: TmuxSessionManager,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "paste-output.txt"
    quoted_output = shlex.quote(str(output_path))
    command = (
        "while IFS= read -r line; do "
        f"printf '%s\\n' \"$line\" >> {quoted_output}; "
        '[ "$line" = done ] && break; '
        "done; tail -f /dev/null"
    )

    await tmux_manager.create_session(name="paste-target", command=command)
    assert await tmux_manager.send_keys("paste-target", "alpha\nbeta\ndone\n")

    await _wait_for(lambda: _path_contains(output_path, "alpha\nbeta\ndone\n"))


async def test_output_reader_streams_multibyte_fifo_data(
    tmux_manager: TmuxSessionManager,
    tmux_config: TmuxConfig,
) -> None:
    chunks: list[str] = []

    async def collect(_run_id: str, data: str) -> None:
        chunks.append(data)

    await tmux_manager.create_session(name="fifo-target")
    reader = TmuxOutputReader(tmux_config)
    reader.set_output_callback(collect)
    started = await reader.start_reader("run-fifo", "fifo-target")
    assert started is True

    try:
        assert await tmux_manager.send_keys(
            "fifo-target", "printf 'fifo-multibyte: cafe é 漢\\n'\n"
        )
        await _wait_for(lambda: "fifo-multibyte: cafe é 漢" in "".join(chunks))
    finally:
        await reader.stop_reader("run-fifo")


async def _spawn_gobby_terminal(
    *,
    runtime: TmuxTerminalRuntime,
    manager: TerminalManager,
    project_id: str,
    machine_id: str,
    command: list[str],
    session_id: str | None = None,
    agent_run_id: str | None = None,
) -> Terminal:
    terminal_id = str(uuid4())
    spawn_key = f"gobby-{terminal_id}"
    pending = manager.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="tmux",
        ownership="gobby",
        spawn_key=spawn_key,
        machine_id=machine_id,
        session_id=session_id,
        agent_run_id=agent_run_id,
    )
    prepared = await runtime.prepare_spawn(
        TerminalSpawnRequest(
            terminal_id=UUID(terminal_id),
            spawn_key=spawn_key,
            command=command,
        )
    )
    assert prepared.stored_locator is not None
    assert prepared.locator_key is not None
    live = manager.promote_to_live(
        pending.id,
        locator=prepared.stored_locator,
        locator_key=prepared.locator_key,
        session_name=spawn_key,
    )
    assert live is not None
    return live


async def test_finalise_kills_remain_on_exit_session_and_agrees_with_terminal_list(
    tmux_manager: TmuxSessionManager,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finalising an agent run leaves no gobby-socket pane without a live row."""
    _stub_runtime_cleanup(monkeypatch)
    monkeypatch.setattr(
        "gobby.agents.terminal_delivery.deliver_and_cleanup_terminal_run",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "gobby.agents.terminal_cleanup.reap_srt_runner_process_tree",
        AsyncMock(),
    )

    runtime = TmuxTerminalRuntime(tmux_manager)
    terminals = TerminalManager(temp_db)
    machine_id = require_machine_id()
    session = session_manager.register(
        external_id="finalise-orphan-session",
        machine_id=machine_id,
        source="claude",
        project_id=sample_project["id"],
    )
    arm = LocalAgentRunManager(temp_db)
    run = arm.create(
        parent_session_id=session.id,
        provider="claude",
        prompt="finalise orphan",
        child_session_id=session.id,
    )
    arm.start(run.id)
    dying = await _spawn_gobby_terminal(
        runtime=runtime,
        manager=terminals,
        project_id=sample_project["id"],
        machine_id=machine_id,
        command=["true"],
        session_id=session.id,
        agent_run_id=run.id,
    )
    survivor = await _spawn_gobby_terminal(
        runtime=runtime,
        manager=terminals,
        project_id=sample_project["id"],
        machine_id=machine_id,
        command=["tail", "-f", "/dev/null"],
        session_id=session.id,
    )
    temp_db.execute(
        "UPDATE agent_runs SET terminal_id = %s WHERE id = %s",
        (dying.id, run.id),
    )
    fetched = arm.get(run.id)
    assert fetched is not None
    run = fetched
    assert await runtime.session_present(dying) is True

    registry = TerminalRuntimeRegistry()
    registry.register(runtime)
    services = TerminalServices(manager=terminals, registry=registry)
    handler = _handler(
        temp_db,
        agent_run_manager=arm,
        session_manager=session_manager,
        terminal_services=services,
    )
    assert await handler.terminalize_successful_run(
        run.id,
        notify_result={"status": "completed"},
        message="done",
    )

    assert await runtime.session_present(dying) is False
    assert await runtime.is_live(survivor) is True
    panes = await tmux_manager.list_panes()
    assert panes is not None
    pane_names = {pane.session_name for pane in panes}
    live_rows = terminals.list_live_by_machine(machine_id)
    row_names = {row.session_name for row in live_rows if row.session_name}
    assert pane_names == row_names == {survivor.session_name}
    assert dying.session_name not in pane_names
    exited = terminals.get(dying.id)
    assert exited is not None and exited.state == "exited"

    config = MagicMock()
    config.host = "localhost"
    config.port = 60888
    config.ping_interval = 30
    config.ping_timeout = 10
    config.max_message_size = 1024
    ws_server = WebSocketServer(config, MagicMock(), AsyncMock(return_value="test-user"))
    ws_server.terminal_manager = terminals
    ws_server.session_manager = session_manager
    ws_server._tmux_mgr_gobby = tmux_manager
    ws_server._tmux_mgr_default = TmuxSessionManager(
        TmuxConfig(socket_name=f"gobby-empty-{uuid4().hex}", config_file="/dev/null")
    )
    ws = MockWebSocket()
    await ws_server._handle_terminal_list(ws, {"type": "terminal_list", "request_id": "proof"})
    page = ws.last_message()

    assert page["type"] == "terminal_list"
    listed_ids = {item["terminal_id"] for item in page["items"]}
    assert listed_ids == {row.id for row in live_rows} == {survivor.id}
