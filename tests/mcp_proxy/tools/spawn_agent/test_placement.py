"""Placed spawn_agent launches: preflight refusals, compensation, and the reply (1.4)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import gobby.terminals.workspace_agent_panes as agent_panes
from gobby.agents import spawn_executor, spawn_in_doubt_owner
from gobby.agents.isolation import IsolationContext, get_isolation_handler
from gobby.agents.sandbox import SandboxConfig
from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import prepare_terminal_spawn
from gobby.agents.spawn_executor_providers import ProviderSpawnPlan
from gobby.agents.spawn_executor_runtime import _runtime_spawn
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.agents.srt_runtime import SandboxLaunch, SrtRuntimeError
from gobby.mcp_proxy.tools.spawn_agent import _implementation as impl
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.mcp_proxy.tools.spawn_agent._placement import preflight_placement
from gobby.servers.websocket.models import WebSocketConfig
from gobby.servers.websocket.server import WebSocketServer
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager, ensure_system_session, system_session_id
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.terminals import Terminal, TerminalManager, mint_terminal_id
from gobby.storage.workspaces import Workspace, WorkspaceManager, WorkspacePane
from gobby.storage.worktrees import LocalWorktreeManager
from gobby.terminals import TerminalRuntimeRegistry
from gobby.terminals.host_client import HostCommandError
from gobby.terminals.in_doubt import in_doubt_spawns
from gobby.terminals.leases import TerminalLeaseRegistry
from gobby.terminals.termination import kill_terminal
from gobby.terminals.workspace_agent_panes import AgentPaneReserver, AgentPlacementError
from gobby.terminals.workspace_contract import WorkspaceEvent, WorkspaceOpError
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.terminals.write_coordinator import WriteCoordinator
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_srt_verifier")]

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000001"
SEAT = "developer-lane"
Executor = Callable[[SpawnRequest], Awaitable[SpawnResult]]


@pytest.fixture(autouse=True)
def _local_machine_identity(
    monkeypatch: pytest.MonkeyPatch, _mock_spawn_machine_id: None
) -> Iterator[None]:
    # Runs after the package conftest's alternate machine id, so this one wins.
    monkeypatch.setattr(impl, "get_machine_id", lambda: LOCAL_MACHINE_ID)
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture(autouse=True)
def _instant_owner_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(delay: float) -> None:
        del delay
        await asyncio.sleep(0)

    monkeypatch.setattr(spawn_in_doubt_owner, "_sleep", no_wait)


class _Isolation:
    """A created worktree whose preparation and removal are counted."""

    def __init__(self, worktree_id: str, cwd: str) -> None:
        self.worktree_id = worktree_id
        self.cwd = cwd
        self.prepared = 0
        self.cleaned = 0
        self.cleanup_error: Exception | None = None

    async def prepare_environment(self, spawn_config: object) -> IsolationContext:
        del spawn_config
        self.prepared += 1
        return IsolationContext(
            cwd=self.cwd,
            branch_name="placed/seat",
            worktree_id=self.worktree_id,
            isolation_type="worktree",
        )

    async def cleanup_environment(self, spawn_config: object) -> None:
        del spawn_config
        self.cleaned += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error

    def build_context_prompt(self, prompt: str, isolation_ctx: object) -> str:
        del isolation_ctx
        return prompt


class _UnkillableRuntime(FakeRuntime):
    """A runtime whose kill leaves the session running.

    The native host raises ``kill_unproven``, since its kill returns only on a
    proven exit.
    """

    async def terminate(self, terminal: Terminal, grace_seconds: float) -> None:
        del terminal, grace_seconds
        self.terminate_started.set()
        raise HostCommandError("kill_unproven")


@dataclass
class _ExecOrderedRuntime(FakeRuntime):
    """Records provider exec into a shared order list."""

    order: list[str] = field(default_factory=list)

    async def prepare_spawn(self, request: Any) -> Any:
        self.order.append("exec")
        return await super().prepare_spawn(request)


@dataclass
class _Harness:
    db: HubDatabase
    workspaces: WorkspaceManager
    terminals: TerminalManager
    sessions: SessionManager
    runs: LocalAgentRunManager
    registry: TerminalRuntimeRegistry
    reserver: AgentPaneReserver
    ops: WorkspaceOps
    runner: MagicMock
    isolation: _Isolation
    events: list[WorkspaceEvent]
    publish_failures: dict[str, Exception]
    project_id: str
    project_path: str
    parent_id: str
    workspace: Workspace
    executor: Executor
    launches: list[SpawnRequest] = field(default_factory=list)
    prepared: list[str] = field(default_factory=list)
    kills: list[str] = field(default_factory=list)
    kill_error: Exception | None = None
    cleanups: list[str] = field(default_factory=list)


def _plan(request: SpawnRequest) -> ProviderSpawnPlan:
    assert request.agent_run_id is not None
    return ProviderSpawnPlan(
        command=["agent"],
        env={},
        launch=cast(SandboxLaunch, None),
        auth_cli="claude",
        child_session_id=request.session_id,
        agent_run_id=request.agent_run_id,
        title="agent",
    )


async def _runtime_launch(request: SpawnRequest) -> SpawnResult:
    """The real placed runtime path: create_pending, bind, prepare, promote."""
    return await _runtime_spawn(request, _plan(request))


def _build(
    *,
    db: HubDatabase,
    project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtimes: tuple[FakeRuntime, ...],
    reserver: AgentPaneReserver | None = None,
) -> _Harness:
    LocalMachineManager(db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID, hostname="local")
    workspaces, terminals = WorkspaceManager(db), TerminalManager(db)
    sessions = SessionManager(db)
    runs = LocalAgentRunManager(db)
    registry = runtime_registry(*runtimes)
    events: list[WorkspaceEvent] = []
    publish_failures: dict[str, Exception] = {}

    async def recording_publish(event: WorkspaceEvent) -> None:
        failure = publish_failures.pop(event["kind"], None)
        if failure is not None:
            raise failure
        events.append(event)

    project_id = str(project["id"])
    parent_id = sessions.register_session(
        external_id=f"placed-parent-{uuid.uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
        title="Runbook pipeline",
    )
    worktree = LocalWorktreeManager(db).create(
        project_id=project_id,
        branch_name="placed/seat",
        worktree_path=str(tmp_path / "placed-wt"),
    )
    child_sessions = ChildSessionManager(sessions)
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.child_session_manager = child_sessions
    runner._child_session_manager = child_sessions
    runner.run_storage = runs
    runner.terminal_manager = terminals
    runner.terminal_runtime_registry = registry
    runner.write_coordinator = None
    runner.agent_lifecycle_monitor = None
    runner.config_runtime = None
    # Rollback cancels the run row as the real runner does.
    runner.cancel_run.side_effect = lambda run_id: runner.run_storage.cancel(run_id) is not None
    isolation = _Isolation(worktree.id, str(tmp_path / "placed-wt"))
    the_reserver = reserver or AgentPaneReserver(
        workspaces=workspaces,
        terminals=terminals,
        registry=registry,
        sessions=sessions,
        publish=recording_publish,
    )
    h = _Harness(
        db=db,
        workspaces=workspaces,
        terminals=terminals,
        sessions=sessions,
        runs=runs,
        registry=registry,
        reserver=the_reserver,
        ops=WorkspaceOps(
            workspaces=workspaces,
            terminals=terminals,
            registry=registry,
            coordinator=WriteCoordinator(
                terminals,
                registry,
                lease_registry=TerminalLeaseRegistry(daemon_epoch="test-epoch"),
            ),
            sessions=sessions,
            publish=recording_publish,
        ),
        runner=runner,
        isolation=isolation,
        events=events,
        publish_failures=publish_failures,
        project_id=project_id,
        project_path=str(tmp_path),
        parent_id=parent_id,
        workspace=workspaces.create(LOCAL_MACHINE_ID, "agents")[0],
        executor=_runtime_launch,
    )

    async def execute(request: SpawnRequest) -> SpawnResult:
        h.launches.append(request)
        return await h.executor(request)

    real_prepare = prepare_terminal_spawn

    def counting_prepare(*args: Any, **kwargs: Any) -> Any:
        h.prepared.append(str(kwargs.get("agent_run_id")))
        return real_prepare(*args, **kwargs)

    real_kill = kill_terminal

    async def recording_kill(manager: Any, kill_registry: Any, terminal: Terminal) -> Any:
        h.kills.append(terminal.id)
        if h.kill_error is not None:
            raise h.kill_error
        return await real_kill(manager, kill_registry, terminal)

    from gobby.mcp_proxy.tools.spawn_agent import _failure_cleanup

    real_cleanup = _failure_cleanup._cleanup_failed_spawn

    async def counting_cleanup(*args: Any, **kwargs: Any) -> None:
        h.cleanups.append(str(args[1]))
        await real_cleanup(*args, **kwargs)

    context = {"id": project_id, "project_path": str(tmp_path)}
    monkeypatch.setattr(impl, "execute_spawn", execute)
    monkeypatch.setattr(impl, "prepare_terminal_spawn", counting_prepare)
    monkeypatch.setattr(impl, "get_isolation_handler", lambda *a, **k: isolation)
    monkeypatch.setattr(impl, "provider_mcp_config_error", lambda *a, **k: None)
    monkeypatch.setattr(impl, "get_project_context", lambda *a, **k: context)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._factory.get_project_context", lambda *a, **k: context
    )
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body", lambda *a, **k: None
    )
    monkeypatch.setattr(agent_panes, "kill_terminal", recording_kill)
    monkeypatch.setattr(_failure_cleanup, "_cleanup_failed_spawn", counting_cleanup)
    monkeypatch.setattr(spawn_executor, "wrap_provider_command", lambda launch, command: command)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._execution.schedule_tmux_health_check",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("gobby.runner_broadcasting.fire_agent_event", lambda *a, **k: None)
    return h


@pytest.fixture
def placed(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _Harness:
    return _build(
        db=temp_db,
        project=sample_project,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runtimes=(FakeRuntime(backend="native"),),
    )


def _tab(h: _Harness, title: str = SEAT) -> dict[str, Any]:
    return {"tab": {"workspace": h.workspace.id, "title": title}}


def _split(pane_id: str, title: str = SEAT) -> dict[str, Any]:
    return {"split": {"pane": pane_id, "axis": "right", "title": title}}


async def _spawn(h: _Harness, placement: dict[str, Any] | None, **overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "prompt": "Run the seat",
        "runner": h.runner,
        "provider": "claude",
        "isolation": "worktree",
        "parent_session_id": h.parent_id,
        "project_path": h.project_path,
        "target_project_id": h.project_id,
        "session_manager": h.sessions,
        "db": h.db,
        "terminal_backend": "native",
        "placement": placement,
        "agent_pane_reserver": h.reserver,
        "project_context_authoritative": True,
    }
    arguments.update(overrides)
    return await spawn_agent_impl(**arguments)


def _registry(h: _Harness, *, task_manager: LocalTaskManager | None = None) -> Any:
    return create_spawn_agent_registry(
        h.runner,
        task_manager=task_manager,
        session_manager=h.sessions,
        db=h.db,
        agent_pane_reserver_resolver=lambda: h.reserver,
    )


def _held_seat(h: _Harness, title: str = SEAT, state: str = "pending") -> WorkspacePane:
    """A tab titled ``title`` whose pane holds a terminal in ``state``."""
    change = h.workspaces.create_tab(
        h.workspace.id, pane_id=str(uuid.uuid4()), project_id=h.project_id, title=title
    )
    terminal_id = mint_terminal_id()
    h.terminals.create_pending(
        terminal_id, h.project_id, "native", "gobby", terminal_id, machine_id=LOCAL_MACHINE_ID
    )
    if state == "exited":
        h.terminals.fail_pending(terminal_id)
    pane = h.workspaces.set_pane_terminal(change.panes[0].id, terminal_id, owns_terminal=True)
    assert pane is not None
    return pane


def _panes(h: _Harness) -> dict[str, str | None]:
    return {pane.id: pane.terminal_id for pane in h.workspaces.list_panes(h.workspace.id)}


def _tabs(h: _Harness) -> set[str]:
    return {tab.id for tab in h.workspaces.list_tabs(h.workspace.id)}


def _terminal_states(h: _Harness) -> dict[str, str]:
    rows = h.db.fetchall("SELECT id, state FROM terminals WHERE project_id = %s", (h.project_id,))
    return {str(row["id"]): str(row["state"]) for row in rows}


def _run_count(h: _Harness) -> int:
    row = h.db.fetchone(
        "SELECT count(*) AS n FROM agent_runs WHERE parent_session_id = %s", (h.parent_id,)
    )
    assert row is not None
    return int(row["n"])


def _run_statuses(h: _Harness) -> list[str]:
    rows = h.db.fetchall(
        "SELECT status FROM agent_runs WHERE parent_session_id = %s", (h.parent_id,)
    )
    return [str(row["status"]) for row in rows]


def _child_sessions(h: _Harness) -> int:
    row = h.db.fetchone(
        "SELECT count(*) AS n FROM sessions WHERE parent_session_id = %s", (h.parent_id,)
    )
    assert row is not None
    return int(row["n"])


def _kinds(h: _Harness) -> list[str]:
    return [event["kind"] for event in h.events]


def _assert_untouched(h: _Harness, panes: dict[str, str | None], terminals: dict[str, str]) -> None:
    """No isolation, child session, run, terminal, pane, event or seat mark exists."""
    assert h.isolation.prepared == 0
    assert h.prepared == []
    assert h.launches == []
    assert _run_count(h) == 0
    assert _child_sessions(h) == 0
    assert _terminal_states(h) == terminals
    assert _panes(h) == panes
    assert h.events == []
    assert h.reserver._seats == {}


def _assert_seat_free(h: _Harness) -> None:
    assert h.reserver._seats == {}
    assert not any(h.workspaces.is_spawn_in_flight(pane_id) for pane_id in _panes(h))


async def _assert_seat_live(h: _Harness) -> None:
    placement = agent_panes.AgentPlacement.parse(_tab(h))
    with pytest.raises(AgentPlacementError) as refused:
        await h.reserver.preflight(f"session:{h.parent_id}", h.project_id, placement)
    assert refused.value.code == "seat_live"


async def _drain_owners() -> None:
    loop = asyncio.get_running_loop()
    for _ in range(50):
        pending = [t for t in spawn_executor._TIMEOUT_CLEANUP_TASKS if t.get_loop() is loop]
        if not pending:
            return
        await asyncio.wait(pending, timeout=5)
    raise AssertionError("late cleanup did not finish")


def _create_launch_terminal(h: _Harness, request: SpawnRequest) -> str:
    assert request.agent_run_id is not None
    terminal_id = mint_terminal_id()
    h.terminals.create_pending(
        terminal_id,
        request.project_id,
        "native",
        "gobby",
        terminal_id,
        machine_id=LOCAL_MACHINE_ID,
        session_id=request.session_id,
        agent_run_id=request.agent_run_id,
    )
    return terminal_id


async def _bind_and_report(request: SpawnRequest, terminal_id: str) -> SpawnResult:
    """Bind the pending row, then return a typed failure without settling it."""
    assert request.agent_run_id is not None and request.placement_binder is not None
    error = "launch failed after bind"
    try:
        await request.placement_binder(terminal_id)
    except Exception as exc:
        error = str(exc)
    return SpawnResult(
        success=False,
        run_id=request.agent_run_id,
        child_session_id=request.session_id,
        status="failed",
        error=error,
        terminal_id=terminal_id,
    )


async def _sandbox_refused(request: SpawnRequest) -> SpawnResult:
    assert request.agent_run_id is not None
    return SpawnResult(
        success=False,
        run_id=request.agent_run_id,
        child_session_id=request.session_id,
        status="failed",
        error="Sandbox startup failed closed",
    )


async def test_refused_placement_has_no_side_effects(placed: _Harness) -> None:
    h = placed
    occupied = _held_seat(h, "occupied-seat")
    other = LocalProjectManager(h.db).create(name="placed-outsider")
    outsider = h.sessions.register_session(
        external_id="placed-outsider",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=other.id,
        title="Outsider",
    )
    panes, terminals = _panes(h), _terminal_states(h)
    cases: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("invalid_placement", {"tab": {"title": SEAT}}, {}),
        ("seat_live", _tab(h, "occupied-seat"), {}),
        ("not_found", {"tab": {"workspace": str(uuid.uuid4()), "title": SEAT}}, {}),
        ("forbidden", _tab(h), {"parent_session_id": outsider}),
    ]
    for code, placement, overrides in cases:
        result = await _spawn(h, placement, **overrides)

        assert result["success"] is False, code
        assert result["placement_error"] == code
        assert isinstance(result["error"], str) and result["error"]
        _assert_untouched(h, panes, terminals)
    assert _panes(h)[occupied.id] == occupied.terminal_id


async def test_placed_launch_requires_managed_srt(
    placed: _Harness, stub_srt_verifier: MagicMock
) -> None:
    h = placed
    panes, terminals = _panes(h), _terminal_states(h)
    disabled = SimpleNamespace(agent_sandbox=SimpleNamespace(enabled=False, backend="srt"))
    native = SimpleNamespace(agent_sandbox=SimpleNamespace(enabled=True, backend="provider-native"))
    for daemon_config in (disabled, native):
        result = await _spawn(h, _tab(h), daemon_config=daemon_config)

        assert result["success"] is False
        assert result["placement_error"] == "sandbox_required"
        _assert_untouched(h, panes, terminals)

    # Configure the module's verifier stub; patching over it would outlive the stub's teardown.
    stub_srt_verifier.side_effect = SrtRuntimeError("managed SRT is not installed")
    result = await _spawn(h, _tab(h))
    assert result["placement_error"] == "sandbox_required"
    _assert_untouched(h, panes, terminals)

    # An unplaced spawn with the same sandbox config is refused by the same gate (1.8).
    unplaced = await _spawn(h, None, daemon_config=disabled)
    assert unplaced["success"] is False
    assert unplaced["error_code"] == "sandbox_required"
    assert "placement_error" not in unplaced
    _assert_untouched(h, panes, terminals)


async def test_wrap_failure_refuses_and_releases_pane(placed: _Harness) -> None:
    h = placed
    h.executor = _sandbox_refused

    result = await _spawn(h, _tab(h))

    assert result["success"] is False
    assert "Sandbox startup failed closed" in result["error"]
    assert len(h.launches) == 1
    assert _terminal_states(h) == {}
    assert _panes(h) == {} and _tabs(h) == set()
    assert "tab.created" not in _kinds(h)
    assert h.cleanups == [result["run_id"]]
    assert h.isolation.cleaned == 1
    assert h.kills == []
    _assert_seat_free(h)


@pytest.mark.parametrize("failure", ["liveness", "start_run"])
async def test_late_returned_failure_releases_pane_once(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    h = placed
    if failure == "liveness":

        async def dead(*args: Any, **kwargs: Any) -> tuple[bool, str | None]:
            return False, None

        monkeypatch.setattr("gobby.mcp_proxy.tools.spawn_agent._execution._terminal_is_live", dead)
    else:

        def start_fails(run_id: str) -> Any:
            raise RuntimeError("start transition failed")

        monkeypatch.setattr(h.runs, "start", start_fails)

    result = await _spawn(h, _tab(h))

    assert result["success"] is False
    [terminal_id] = _terminal_states(h)
    assert _terminal_states(h) == {terminal_id: "exited"}
    assert _panes(h) == {} and _tabs(h) == set()
    assert h.cleanups == [result["run_id"]]
    assert h.kills == []
    _assert_seat_free(h)


@pytest.mark.parametrize(
    "exit_kind", ["exception", "kill_fails", "cancelled", "busy", "cleanup_fails"]
)
async def test_exceptions_and_cancellation_release_pane(placed: _Harness, exit_kind: str) -> None:
    h = placed
    bound = asyncio.Event()
    launched: list[str] = []
    others: list[WorkspacePane] = []

    async def bind_then_fail(request: SpawnRequest) -> SpawnResult:
        assert request.placement_binder is not None
        terminal_id = _create_launch_terminal(h, request)
        launched.append(terminal_id)
        await request.placement_binder(terminal_id)
        bound.set()
        if exit_kind == "cancelled":
            await asyncio.Event().wait()
        raise RuntimeError("provider exploded")

    async def bind_busy(request: SpawnRequest) -> SpawnResult:
        terminal_id = _create_launch_terminal(h, request)
        launched.append(terminal_id)
        change = h.workspaces.create_tab(
            h.workspace.id, pane_id=str(uuid.uuid4()), project_id=h.project_id, title="other"
        )
        other = h.workspaces.set_pane_terminal(change.panes[0].id, terminal_id, owns_terminal=True)
        assert other is not None
        others.append(other)
        return await _bind_and_report(request, terminal_id)

    h.executor = bind_busy if exit_kind == "busy" else bind_then_fail
    if exit_kind == "kill_fails":
        h.kill_error = RuntimeError("host kill failed")
    if exit_kind == "cleanup_fails":
        h.isolation.cleanup_error = RuntimeError("worktree removal failed")

    result: dict[str, Any] = {}
    if exit_kind == "cancelled":
        spawn = asyncio.create_task(_spawn(h, _tab(h)))
        await asyncio.wait_for(bound.wait(), timeout=5)
        spawn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawn
    else:
        result = await _spawn(h, _tab(h))
        assert result["success"] is False

    [terminal_id] = launched
    assert len(h.cleanups) == 1
    _assert_seat_free(h)
    if exit_kind == "busy":
        assert result["placement_error"] == "busy"
        assert h.kills == []
        [other] = others
        assert _panes(h) == {other.id: terminal_id}
        return
    assert h.kills == [terminal_id]
    if exit_kind == "kill_fails":
        # The kill failed, so the pane stays bound and holds the seat for terminal_kill.
        [pane_id] = _panes(h)
        assert _panes(h) == {pane_id: terminal_id}
        assert _terminal_states(h)[terminal_id] in {"pending", "orphaned"}
        await _assert_seat_live(h)
        return
    assert _terminal_states(h) == {terminal_id: "exited"}
    assert _panes(h) == {} and _tabs(h) == set()


@pytest.mark.parametrize("kind", ["tab", "split"])
async def test_placed_spawn_reply_carries_refs(placed: _Harness, kind: str) -> None:
    h = placed
    beside = _held_seat(h, "beside", state="exited")
    placement = _tab(h) if kind == "tab" else _split(beside.id)
    tabs_before = _tabs(h)

    result = await _spawn(h, placement)

    assert result["success"] is True, result
    assert result["run_id"] not in impl._spawn_background_tasks
    terminal_id = result["terminal_id"]
    assert _terminal_states(h)[terminal_id] == "live"
    [pane_id] = [pane for pane, bound in _panes(h).items() if bound == terminal_id]
    assert result["pane_ref"].startswith(f"{result['tab_ref']}:")
    assert result["tab_ref"].startswith(f"{result['workspace']}:")
    assert result["workspace"].count(":") == 1
    assert result["pane_ref"].count(":") == 3
    assert _kinds(h) == ["tab.created" if kind == "tab" else "pane.added"]
    [event] = h.events
    assert [pane["id"] for pane in event["panes"]] == [pane_id]
    if kind == "tab":
        assert len(_tabs(h) - tabs_before) == 1
    else:
        assert _tabs(h) == tabs_before
    _assert_seat_free(h)
    await _assert_seat_live(h)


@pytest.mark.parametrize("refusal", ["slot", "lease", "active_task", "seat_race"])
async def test_late_refusals_leave_no_pane(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    h = placed
    if refusal == "slot":

        @asynccontextmanager
        async def full(**kwargs: Any) -> AsyncIterator[dict[str, Any] | None]:
            yield {"success": False, "error": "max active agents reached"}

        monkeypatch.setattr(impl, "reserve_agent_slot", full)
    elif refusal == "lease":

        async def leased(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            await kwargs["cleanup"]()
            return {"success": False, "error": "task already has an agent spawn in progress"}

        monkeypatch.setattr(impl, "admit_task_spawn", leased)
    elif refusal == "active_task":

        async def active(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
            await kwargs["cleanup"]()
            return {"success": True, "skipped": True, "run_id": "run-active", "status": "running"}

        monkeypatch.setattr(impl, "admit_task_spawn", active)
    else:
        real_reserve = h.reserver.reserve

        async def lost_race(resolved: Any, *, worktree_id: str | None) -> Any:
            _held_seat(h)  # A concurrent launch took the seat after preflight.
            return await real_reserve(resolved, worktree_id=worktree_id)

        monkeypatch.setattr(h.reserver, "reserve", lost_race)

    panes = _panes(h)
    result = await _spawn(h, _tab(h))

    assert result["success"] is False
    assert h.launches == []
    assert h.isolation.prepared == 1 and h.isolation.cleaned == 1
    assert h.events == []
    if refusal == "seat_race":
        assert result["placement_error"] == "seat_live"
        assert h.cleanups == [result["run_id"]]
        run = h.runs.get(result["run_id"])
        assert run is not None and run.status == "cancelled"
        assert _child_sessions(h) == 0
        assert len(_panes(h)) == 1  # Only the winner's seat.
    else:
        assert _panes(h) == panes
    if refusal == "active_task":
        assert result["placement_error"] == "task_active"
        assert result["run_id"] == "run-active"
    _assert_seat_free(h)


async def test_parent_and_project_provenance(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.mcp_proxy.tools.spawn_agent import _factory

    h = placed
    parent_ctx = {"id": h.project_id, "project_path": "/parent"}
    ambient_ctx = {"id": h.project_id, "project_path": "/ambient"}
    monkeypatch.setattr(_factory, "_context_from_project_path", lambda path: {"id": h.project_id})
    monkeypatch.setattr(_factory, "get_project_context", lambda *a, **k: ambient_ctx)

    def resolve(*, project_path: str | None, parent: dict[str, Any] | None) -> bool:
        monkeypatch.setattr(_factory, "_parent_session_project_context", lambda **k: parent)
        ctx, path, authoritative = _factory._resolve_spawn_project_context_with_provenance(
            project_path=project_path,
            parent_session_id=h.parent_id,
            session_manager=h.sessions,
            db=h.db,
        )
        assert (ctx, path) == _factory._resolve_spawn_project_context(
            project_path=project_path,
            parent_session_id=h.parent_id,
            session_manager=h.sessions,
            db=h.db,
        )
        return authoritative

    assert resolve(project_path="/explicit", parent=None) is True
    assert resolve(project_path=None, parent=parent_ctx) is True
    assert resolve(project_path=None, parent={"project_id": h.project_id}) is True
    assert resolve(project_path=None, parent=None) is False

    panes, terminals = _panes(h), _terminal_states(h)
    ensure_system_session(h.db)
    system_parent = system_session_id(LOCAL_MACHINE_ID)
    for overrides in (
        {"parent_session_id": system_parent},
        {"project_context_authoritative": False},
    ):
        result = await _spawn(h, _tab(h), **overrides)
        assert result["success"] is False
        assert result["placement_error"] == "parent_unresolved"
        _assert_untouched(h, panes, terminals)
    # Without a machine id the parent cannot be told apart from the system session.
    unknown_machine = await preflight_placement(
        _tab(h),
        reserver=h.reserver,
        sandbox_config=SandboxConfig(enabled=True, backend="srt"),
        parent_session_id=h.parent_id,
        machine_id=None,
        project_id=h.project_id,
        project_context_authoritative=True,
    )
    assert isinstance(unknown_machine, dict)
    assert unknown_machine["placement_error"] == "parent_unresolved"
    _assert_untouched(h, panes, terminals)

    pipeline_child = h.sessions.register_session(
        external_id=f"pipeline-{uuid.uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="pipeline",
        project_id=h.project_id,
        parent_session_id=system_parent,
        title="Pipeline run",
    )
    accepted = await _spawn(h, _tab(h), parent_session_id=pipeline_child)
    assert accepted["success"] is True, accepted
    assert accepted["pane_ref"]


@pytest.mark.parametrize("reuse", [False, True])
async def test_failed_placed_spawn_cleans_created_isolation_only(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch, reuse: bool
) -> None:
    h = placed
    h.executor = _sandbox_refused
    overrides: dict[str, Any] = {}
    worktree_storage = MagicMock()
    git_manager = MagicMock(repo_path=h.project_path)
    if reuse:
        existing = SimpleNamespace(
            id=h.isolation.worktree_id,
            worktree_path=h.project_path,
            branch_name="placed/seat",
            base_branch="main",
            project_id=h.project_id,
        )
        worktree_storage.resolve_reference.return_value = existing.id
        worktree_storage.get.return_value = existing

        async def reused(**kwargs: Any) -> tuple[IsolationContext, Any]:
            return (
                IsolationContext(
                    cwd=existing.worktree_path,
                    branch_name=existing.branch_name,
                    worktree_id=existing.id,
                    isolation_type="worktree",
                    extra={"reused_worktree": True},
                ),
                get_isolation_handler("none"),
            )

        monkeypatch.setattr(impl, "prepare_reused_worktree", reused)
        overrides = {
            "worktree_id": existing.id,
            "worktree_storage": worktree_storage,
            "git_manager": git_manager,
        }

    result = await _spawn(h, _tab(h), **overrides)

    assert result["success"] is False
    assert "Sandbox startup failed closed" in result["error"]
    assert _panes(h) == {}
    if reuse:
        assert h.isolation.prepared == 0 and h.isolation.cleaned == 0
        worktree_storage.delete.assert_not_called()
        git_manager.delete_worktree.assert_not_called()
    else:
        assert h.isolation.prepared == 1 and h.isolation.cleaned == 1


async def test_concurrent_placed_spawns_share_one_reserver(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    broadcasts: list[WorkspaceEvent] = []

    async def broadcast(self: WebSocketServer, event: WorkspaceEvent) -> None:
        order.append(event["kind"])
        broadcasts.append(event)

    monkeypatch.setattr(WebSocketServer, "broadcast_workspace_event", broadcast)
    ws_config = MagicMock(spec=WebSocketConfig)
    ws_config.host, ws_config.port = "localhost", 60888
    ws_config.ping_interval, ws_config.ping_timeout, ws_config.max_message_size = 30, 10, 1024
    native = _ExecOrderedRuntime(backend="native", order=order)
    registry = runtime_registry(native)
    server = WebSocketServer(ws_config, MagicMock(), MagicMock())
    server.session_manager = SessionManager(temp_db)
    server.configure_terminals(
        TerminalManager(temp_db),
        registry,
        lease_registry=MagicMock(),
        write_coordinator=MagicMock(),
        workspace_manager=WorkspaceManager(temp_db),
    )
    reserver = server.agent_pane_reserver
    assert reserver is not None
    h = _build(
        db=temp_db,
        project=sample_project,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runtimes=(native,),
        reserver=reserver,
    )
    h.runner.terminal_runtime_registry = registry
    tool = _registry(h)
    arguments = {
        "prompt": "Run the seat",
        "provider": "claude",
        "isolation": "worktree",
        "parent_session_id": h.parent_id,
        "terminal_backend": "native",
        "placement": _tab(h),
    }

    first, second = await asyncio.gather(
        tool.call("spawn_agent", dict(arguments)), tool.call("spawn_agent", dict(arguments))
    )

    winners = [r for r in (first, second) if r["success"] is True]
    losers = [r for r in (first, second) if r["success"] is not True]
    assert len(winners) == 1 and len(losers) == 1, (first, second)
    assert losers[0]["placement_error"] == "seat_live"
    assert winners[0]["pane_ref"]
    assert [event["kind"] for event in broadcasts] == ["tab.created"]
    assert order == ["tab.created", "exec"]
    assert len(_panes(h)) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_reserve_failure_and_cancellation_clean_dispatch_state(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    h = placed
    real_create_tab = h.workspaces.create_tab
    started, unblock = threading.Event(), threading.Event()

    def create_tab(*args: Any, **kwargs: Any) -> Any:
        started.set()
        if not cancel:
            raise RuntimeError("tab insert failed")
        unblock.wait(5)
        return real_create_tab(*args, **kwargs)

    monkeypatch.setattr(h.workspaces, "create_tab", create_tab)
    spawn = asyncio.create_task(_spawn(h, _tab(h)))
    if cancel:
        await asyncio.to_thread(started.wait, 5)
        spawn.cancel()
        unblock.set()
        with pytest.raises(asyncio.CancelledError):
            await spawn
    else:
        result = await spawn
        assert result["success"] is False
        assert "tab insert failed" in result["error"]

    assert started.is_set()
    assert h.launches == []
    assert len(h.cleanups) == 1
    [run_id] = h.cleanups
    run = h.runs.get(run_id)
    assert run is not None and run.status == "cancelled"
    assert _child_sessions(h) == 0
    assert h.isolation.prepared == 1 and h.isolation.cleaned == 1
    assert _panes(h) == {} and _tabs(h) == set()
    _assert_seat_free(h)


@pytest.mark.parametrize("cleanup_reaches_terminal", [True, False])
async def test_bind_publish_failure_keeps_release_kill_backstop(
    placed: _Harness, monkeypatch: pytest.MonkeyPatch, cleanup_reaches_terminal: bool
) -> None:
    h = placed
    h.publish_failures["tab.created"] = RuntimeError("broadcast failed")
    released: list[str | None] = []
    real_release = h.reserver.release

    async def recording_release(reserved: Any, *, terminal_id: str | None) -> None:
        released.append(terminal_id)
        await real_release(reserved, terminal_id=terminal_id)

    monkeypatch.setattr(h.reserver, "release", recording_release)

    async def publish_fails(request: SpawnRequest) -> SpawnResult:
        return await _bind_and_report(request, _create_launch_terminal(h, request))

    h.executor = publish_fails
    if not cleanup_reaches_terminal:
        # Cleanup's terminate step cannot read the terminal, so it never reaches it.
        failing = MagicMock(wraps=h.terminals)
        failing.get.side_effect = RuntimeError("terminal read failed")
        h.runner.terminal_manager = failing

    result = await _spawn(h, _tab(h))

    assert result["success"] is False
    [terminal_id] = _terminal_states(h)
    assert released == [terminal_id]
    assert len(h.cleanups) == 1
    assert _terminal_states(h) == {terminal_id: "exited"}
    assert _panes(h) == {} and _tabs(h) == set()
    assert h.kills == ([] if cleanup_reaches_terminal else [terminal_id])
    _assert_seat_free(h)


async def test_duplicate_placed_request_precedence(placed: _Harness) -> None:
    h = placed
    tasks = LocalTaskManager(h.db)
    task = tasks.create_task(
        h.project_id, "Seat task", validation_criteria="The seat agent finishes its task."
    )
    active = SimpleNamespace(
        id="run-active",
        status="running",
        child_session_id=None,
        parent_session_id="someone-else",
        task_id=task.id,
        agent_name="developer",
    )
    runs = MagicMock(wraps=h.runs)
    runs.has_active_run_for_task.return_value = True
    runs.list_active_global.return_value = [active]
    h.runner.run_storage = runs
    tool = _registry(h, task_manager=tasks)
    arguments: dict[str, Any] = {
        "prompt": "Run the seat",
        "provider": "claude",
        "isolation": "worktree",
        "task_id": task.id,
        "parent_session_id": h.parent_id,
        "terminal_backend": "native",
    }

    free_seat = await tool.call("spawn_agent", {**arguments, "placement": _tab(h)})

    assert free_seat == {"success": False, "placement_error": "task_active", "run_id": "run-active"}
    assert h.isolation.prepared == 1 and h.isolation.cleaned == 1
    assert _panes(h) == {}

    occupied = _held_seat(h)
    held_seat = await tool.call("spawn_agent", {**arguments, "placement": _tab(h)})
    assert held_seat["placement_error"] == "seat_live"
    assert h.isolation.prepared == 1

    unplaced = await tool.call("spawn_agent", arguments)
    assert unplaced["success"] is True and unplaced["skipped"] is True
    assert unplaced["run_id"] == "run-active"
    assert h.launches == []
    assert _panes(h) == {occupied.id: occupied.terminal_id}


@pytest.mark.parametrize("proven", [True, False])
@pytest.mark.parametrize("cancel_while_held", [False, True])
async def test_placed_timeout_race_keeps_pane_until_owner_settles(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proven: bool,
    cancel_while_held: bool,
) -> None:
    hold = asyncio.Event()
    runtime_type = FakeRuntime if proven else _UnkillableRuntime
    runtime = runtime_type(backend="native", spawn_hold=hold)
    h = _build(
        db=temp_db,
        project=sample_project,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runtimes=(runtime,),
    )

    result = await _spawn(h, _tab(h), timeout=0.05)

    assert result["success"] is False
    [terminal_id] = _terminal_states(h)
    assert _terminal_states(h) == {terminal_id: "pending"}
    assert in_doubt_spawns.holds(terminal_id)
    [pane_id] = _panes(h)
    assert _panes(h) == {pane_id: terminal_id}
    assert h.isolation.cleaned == 0
    assert len(h.cleanups) == 1
    # The run rollback waits for the owner, so the held row stays pending.
    assert _run_statuses(h) == ["pending"]
    assert _child_sessions(h) == 1
    if cancel_while_held:
        runs = h.runner.run_storage.list_by_session(h.parent_id)
        assert len(runs) == 1
        assert h.runner.run_storage.cancel(runs[0].id) is not None
        assert _terminal_states(h) == {terminal_id: "pending"}
    with pytest.raises(WorkspaceOpError) as refused:
        await h.ops.pane_close("operator", pane_id)
    assert refused.value.code == "busy"
    assert _panes(h) == {pane_id: terminal_id}
    await _assert_seat_live(h)

    hold.set()
    await _drain_owners()

    assert not in_doubt_spawns.holds(terminal_id)
    assert _run_statuses(h) == ["cancelled"]
    assert _child_sessions(h) == 0
    h.workspaces.sweep_dead_panes(h.workspace.id)
    if proven:
        assert _terminal_states(h) == {terminal_id: "exited"}
        assert h.isolation.cleaned == 1
        assert _panes(h) == {}
        return
    assert _terminal_states(h)[terminal_id] == "orphaned"
    assert h.isolation.cleaned == 0
    assert _panes(h) == {pane_id: terminal_id}


async def test_unplaced_timeout_rolls_back_at_once_without_a_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hold = asyncio.Event()
    runtime = FakeRuntime(backend="native", spawn_hold=hold)
    h = _build(
        db=temp_db,
        project=sample_project,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runtimes=(runtime,),
    )

    result = await _spawn(h, None, timeout=0.05)
    [launch] = [
        task
        for key, task in impl._spawn_background_tasks.items()
        if key.startswith(f"{result['run_id']}:")
    ]
    await launch

    # No owner holds an unplaced row, so the rollback does not wait for the prepare.
    [terminal_id] = _terminal_states(h)
    assert not in_doubt_spawns.holds(terminal_id)
    assert _terminal_states(h) == {terminal_id: "exited"}
    assert _run_statuses(h) == ["cancelled"]
    assert _child_sessions(h) == 0
    assert len(h.cleanups) == 1

    hold.set()
    # The prepare's done-callback schedules the late cleanup a loop turn later.
    for _ in range(50):
        if spawn_executor._TIMEOUT_CLEANUP_TASKS:
            break
        await asyncio.sleep(0)
    assert spawn_executor._TIMEOUT_CLEANUP_TASKS
    await _drain_owners()

    # The late cleanup kills what the prepare created.
    assert _terminal_states(h) == {terminal_id: "exited"}
    assert runtime.killed_host_ids == ["ht-1"]
