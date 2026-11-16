"""Durable reply waits wake a native Codex terminal after an isolated restart."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager
from tests._timing import wait_for_awaited_condition, wait_for_condition
from tests.e2e.conftest import (
    CLIEventSimulator,
    DaemonInstance,
    MCPTestClient,
    copy_daemon_api_key,
    create_host_socket_dir,
    stop_terminal_host,
)
from tests.e2e.terminal_viewer_support import (
    APPROVAL_PROMPT,
    E2E_PROJECT_ID,
    MACHINE_ID,
    _gterm_bin_dir,
    _wait_for_host,
)
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.e2e


@pytest.fixture
def e2e_pre_daemon_setup(
    postgres_db: Any,
    e2e_config: tuple[Path, int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv("GOBBY_NATIVE_BIN_DIR", str(_gterm_bin_dir()))
    socket_dir = create_host_socket_dir(Path("/tmp"), prefix="")
    daemon_home = e2e_config[0].parent
    for directory in (daemon_home, daemon_home / ".gobby", socket_dir):
        directory.mkdir(exist_ok=True)
        copy_daemon_api_key(daemon_home, directory)
    from gobby.storage.config_mutations import ConfigMutations, ConfigPatch

    try:
        mutations = ConfigMutations(postgres_db)
        mutations.patch_internal(
            expected_revision=mutations.repository.current_revision(),
            patch=ConfigPatch(values={"terminal_host.socket_dir": str(socket_dir)}),
            source="e2e-native-reply-wait",
        )
        monkeypatch.setenv("GOBBY_E2E_HOST_SOCKET_DIR", str(socket_dir))
        yield
    finally:
        stop_terminal_host(socket_dir)
        shutil.rmtree(socket_dir, ignore_errors=True)


_CLI_SCRIPT = f"""\
import sys
import termios

PROMPT = {APPROVAL_PROMPT!r}
fd = sys.stdin.fileno()
attrs = termios.tcgetattr(fd)
attrs[3] &= ~termios.ECHO
termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
sys.stdout.write("GOBBY-EXT-BOOT\\n")
if "--codex-idle" in sys.argv:
    sys.stdout.write("› \\n  GPT-6.1-Sol · 80% context left\\n  ? for shortcuts\\n")
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
    elif text == "SHOW_CODEX_IDLE":
        sys.stdout.write("\\n" * 80 + "› \\n  GPT-6.1-Sol · 80% context left\\n  ? for shortcuts\\n")
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


@pytest.fixture
def installed_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install this interpreter as ``codex`` on the isolated daemon's PATH.

    A wake types only into a foreground process that is the build the daemon's PATH
    resolves for an agent CLI. The scripted Codex runs on this interpreter, so the
    daemon must resolve ``codex`` to it. Requested before the daemon, which copies PATH.
    """
    bin_dir = tmp_path / "agent-bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.symlink_to(Path(sys.executable).resolve())
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return codex


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["replied", "timeout"])
async def test_codex_reply_wait_reaches_terminal_after_isolated_restart(
    installed_codex: Path,
    daemon_instance: DaemonInstance,
    daemon_client: httpx.Client,
    cli_events: CLIEventSimulator,
    mcp_client: MCPTestClient,
    postgres_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: str,
) -> None:
    from gobby.agents.detection.registry import DetectionManifestRegistry
    from gobby.agents.idle_detector import COMPOSER_PROBE_LINES, IdleDetector, composer_text
    from gobby.shutdown_intent import ShutdownIntent, write_shutdown_intent
    from tests.e2e.test_terminal_client_stack import _open_control, _ws_create

    patch_local_machine_id(monkeypatch, MACHINE_ID)
    _wait_for_host(daemon_client, daemon_instance)
    script = tmp_path / "codex-idle.py"
    script.write_text(_CLI_SCRIPT)
    created = await _ws_create(daemon_instance, [str(installed_codex), str(script), "--codex-idle"])
    assert created.get("success") is True, created
    terminal_id = str(created["terminal_id"])
    terminals = TerminalManager(postgres_db)
    terminal = terminals.get(terminal_id)
    assert terminal is not None and terminal.backend == "native"
    assert terminal.locator is not None
    host_terminal_id = str(terminal.locator["host_terminal_id"])
    waiter = cli_events.register_session(
        external_id=f"codex-restart-{uuid.uuid4().hex}",
        source="codex",
        project_id=E2E_PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
    )["id"]
    owner = cli_events.register_session(
        external_id=f"owner-restart-{uuid.uuid4().hex}",
        source="codex",
        project_id=E2E_PROJECT_ID,
        cwd=str(daemon_instance.project_dir),
    )["id"]
    sessions = SessionManager(postgres_db)
    assert (
        sessions.update(
            waiter, terminal_context={"gobby_terminal_id": terminal_id}, status="paused"
        )
        is not None
    )
    assert terminals.bind_session(terminal_id, waiter, E2E_PROJECT_ID) is not None
    assert terminal.machine_id == MACHINE_ID
    socket_dir = Path(os.environ["GOBBY_E2E_HOST_SOCKET_DIR"])
    control = await _open_control(socket_dir)

    async def idle_ready() -> bool:
        snapshot = await control.snapshot(host_terminal_id)
        return "80% context left" in str(snapshot.get("text", ""))

    try:
        await wait_for_awaited_condition(
            idle_ready,
            timeout=10,
            interval=0.1,
            description="isolated native Codex idle prompt",
        )
        idle_snapshot = await control.snapshot(
            host_terminal_id, mode="ansi", max_lines=COMPOSER_PROBE_LINES
        )
        idle_text = composer_text(str(idle_snapshot.get("text", "")))
        registry = DetectionManifestRegistry(postgres_db)
        assert registry.for_provider("codex") is not None, "Isolated install lacks Codex manifest"
        detector = IdleDetector(registry, "codex")
        assert detector.composer_read(idle_text).state == "empty"
        assert detector.turn_in_flight_fingerprint(idle_text) is None
    finally:
        await control.close()
    mcp_client.session_id = waiter
    registration = mcp_client.call_tool(
        server_name="gobby-agents",
        tool_name="wait_for_coordination",
        arguments={"owner_session": owner, "reply": True, "timeout": 120},
    )
    result = registration.get("result", registration)
    assert result["outcome"] == "waiting", registration
    wait_id = result["wait_id"]
    # Match the restart path: STOP adds a critical-hook grace period. Stop
    # only the runner; fixture stop() also kills the persistent native PTY.
    write_shutdown_intent(
        "codex-restart-proof", ShutdownIntent.RESTART, home=daemon_instance.gobby_home
    )
    daemon_instance.process.terminate()
    try:
        # The runner has a 17s async shutdown deadline followed by bounded
        # finalizers. Allow scheduling headroom, while retaining a hang bound.
        await asyncio.to_thread(daemon_instance.process.wait, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"Isolated runner did not stop for restart; pid={daemon_instance.pid}; "
            f"logs={daemon_instance.read_logs()}; errors={daemon_instance.read_error_logs()}"
        ) from exc
    daemon_instance.restart()
    _wait_for_host(daemon_client, daemon_instance)
    if outcome == "timeout":
        # Start re-arms a deadline that lapsed in downtime, so the deadline lapses after
        # the restart. Resolving it changes its outcome, and the schema trigger notifies
        # the restarted delivery service, which wakes the waiter.
        with postgres_db.transaction() as conn:
            conn.execute(
                "UPDATE coordination_waits SET expires_at = clock_timestamp() - interval "
                "'1 second' WHERE id = %s",
                (wait_id,),
            )
            conn.execute("SELECT resolve_coordination_wait(%s)", (wait_id,))
    if outcome == "replied":
        mcp_client.session_id = owner
        sent = mcp_client.call_tool(
            server_name="gobby-agents",
            tool_name="send_message",
            arguments={
                "target": "session",
                "target_id": waiter,
                "content": "post-restart durable reply",
                "wake": False,
            },
        )
        assert sent["success"] is True, sent
    wait_for_condition(
        lambda: (
            postgres_db.fetchone("SELECT outcome FROM coordination_waits WHERE id = %s", (wait_id,))
            or {}
        ).get("outcome")
        == outcome,
        timeout=20,
        interval=0.1,
        description=f"durable wait {outcome}",
    )
    control = await _open_control(socket_dir)

    async def wake_received() -> bool:
        snapshot = await control.snapshot(host_terminal_id)
        return "ECHO:[Gobby] Check messages" in str(snapshot.get("text", ""))

    try:
        await wait_for_awaited_condition(
            wake_received,
            timeout=30,
            interval=0.2,
            description="post-restart wait wake reached native Codex pane",
        )
    except AssertionError as exc:
        snapshot = await control.snapshot(host_terminal_id)
        session = sessions.get(waiter)
        status = None if session is None else session.status
        raise AssertionError(f"{exc}; session_status={status}; snapshot={snapshot}") from exc
    finally:
        await control.close()
    assert sessions.get(waiter) is not None
    restarted_terminal = terminals.get(terminal_id)
    assert restarted_terminal is not None and restarted_terminal.state == "live"
