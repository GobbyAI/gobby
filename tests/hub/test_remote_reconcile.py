"""The hub settles remote runs and terminals from the node's reports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from gobby.agents.agent_health import AgentHealthMonitor
from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.config.terminals import TerminalConfig
from gobby.config.tmux import TmuxConfig
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_coordinator import SessionCoordinator
from gobby.hooks.session_types import HookSessionManager
from gobby.hub import node_reports
from gobby.hub.node_client import NodeCommandError
from gobby.hub.node_commands import ReportAgent, ReportBody, ReportExit
from gobby.hub.remote_reconcile import RemoteRunReconciler
from gobby.sessions.turn_lifecycle import TurnDisposition
from gobby.storage.agents import STATUS_ERROR, STATUS_SUCCESS, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.utils.machine_id import require_machine_id
from tests.agents.test_lifecycle_monitor import (
    DETECTION_REGISTRY,
    LifecycleRuntime,
    _fake_terminal_services,
)
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

HOST_EPOCH = 7
OLDER_EPOCH = 6
NO_ACTIVITY = "no activity"
DIED = "terminal session died unexpectedly"
ExitReason = Literal["prepare_failed", "prepare_timeout", "launch_failed", "cancelled", "exited"]


@pytest.fixture(autouse=True)
def _fresh_inbox() -> Any:
    node_reports.node_report_inbox.cache_clear()
    yield
    node_reports.node_report_inbox.cache_clear()


@pytest.fixture
def remote_machine(temp_db: HubDatabase) -> str:
    machine_id = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(machine_id, TEST_USER_ID)
    return machine_id


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    client = AsyncMock()
    monkeypatch.setattr("gobby.hub.remote_reconcile.node_command_client", lambda: client)
    return client


@pytest.fixture
def cleanup() -> AsyncMock:
    return AsyncMock()


def _reconciler(
    temp_db: HubDatabase,
    cleanup: AsyncMock,
    *,
    dead_reason: Any = None,
    in_doubt_seconds: float = 150.0,
) -> RemoteRunReconciler:
    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return RemoteRunReconciler(
        run_manager=LocalAgentRunManager(temp_db),
        terminals=TerminalManager(temp_db),
        cleanup_agent=cleanup,
        dead_reason=dead_reason or AsyncMock(return_value=DIED),
        run_db=run_db,
        in_doubt_seconds=in_doubt_seconds,
    )


@pytest.fixture
def reconciler(temp_db: HubDatabase, cleanup: AsyncMock) -> RemoteRunReconciler:
    return _reconciler(temp_db, cleanup)


def _run(
    temp_db: HubDatabase,
    project_id: str,
    machine_id: str,
    *,
    status: str,
    provider: str = "claude",
    task_id: str | None = None,
    resume_metadata: dict[str, Any] | None = None,
) -> Any:
    runs = LocalAgentRunManager(temp_db)
    parent = str(uuid4())
    temp_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, status)"
        " VALUES (%s, %s, %s, 'claude', %s, 'active')",
        (parent, f"parent-{parent}", machine_id, project_id),
    )
    run = runs.create(
        parent_session_id=parent,
        provider=provider,
        prompt="do it",
        machine_id=machine_id,
        task_id=task_id,
        resume_metadata_json=resume_metadata,
    )
    temp_db.execute("UPDATE agent_runs SET status = %s WHERE id = %s", (status, run.id))
    refreshed = runs.get(run.id)
    assert refreshed is not None
    return refreshed


def _child_session(
    temp_db: HubDatabase,
    project_id: str,
    machine_id: str,
    run_id: str,
    *,
    source: str = "claude",
) -> str:
    session_id = str(uuid4())
    temp_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, status,"
        " agent_run_id) VALUES (%s, %s, %s, %s, %s, 'active', %s)",
        (session_id, f"child-{session_id}", machine_id, source, project_id, run_id),
    )
    temp_db.execute(
        "UPDATE agent_runs SET child_session_id = %s WHERE id = %s", (session_id, run_id)
    )
    return session_id


def _pending(
    temp_db: HubDatabase,
    project_id: str,
    machine_id: str,
    run_id: str,
    session_id: str | None = None,
) -> str:
    terminal_id = str(uuid4())
    TerminalManager(temp_db).create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=f"gobby-{terminal_id}",
        machine_id=machine_id,
        session_id=session_id,
        agent_run_id=run_id,
    )
    return terminal_id


def _state(temp_db: HubDatabase, terminal_id: str) -> str:
    terminal = TerminalManager(temp_db).get(terminal_id)
    assert terminal is not None
    return terminal.state


def _awaited(mock: AsyncMock) -> Any:
    call = mock.await_args
    assert call is not None
    return call


def _host(terminal_id: str) -> str:
    return f"host-{terminal_id}"


def _live(
    temp_db: HubDatabase,
    project_id: str,
    machine_id: str,
    run_id: str,
    session_id: str | None = None,
    *,
    host_epoch: int = HOST_EPOCH,
) -> str:
    terminal_id = _pending(temp_db, project_id, machine_id, run_id, session_id)
    host = _host(terminal_id)
    TerminalManager(temp_db).promote_to_live(
        terminal_id,
        locator={"host_terminal_id": host},
        locator_key=native_locator_key(str(host_epoch), host),
        host_epoch=str(host_epoch),
    )
    return terminal_id


def _age_pending(
    temp_db: HubDatabase,
    terminal_id: str,
    age: timedelta = timedelta(hours=1),
) -> None:
    temp_db.execute(
        "UPDATE terminals SET attempt_started_at = attempt_started_at - %s WHERE id = %s",
        (age, terminal_id),
    )


def _agent(run_id: str, terminal_id: str, pid: int = 4321) -> ReportAgent:
    return ReportAgent(
        run_id=run_id,
        terminal_id=terminal_id,
        pid=pid,
        locator=_host(terminal_id),
        locator_key=native_locator_key(str(HOST_EPOCH), _host(terminal_id)),
        workspace=None,
        pane=None,
        sandbox=None,
    )


def _exit(
    run_id: str,
    terminal_id: str,
    *,
    launched: bool,
    reason: ExitReason,
    exit_code: int | None = None,
) -> ReportExit:
    return ReportExit(
        run_id=run_id,
        terminal_id=terminal_id,
        launched=launched,
        exit_code=exit_code,
        reason=reason,
        workspace=None,
        sandbox=None,
    )


def _feed(
    machine_id: str,
    revision: int,
    *,
    agents: list[ReportAgent] | None = None,
    exits: list[ReportExit] | None = None,
    accepted: list[str] | None = None,
) -> None:
    inbox = node_reports.node_report_inbox()
    report = ReportBody(
        v=1,
        hub_epoch=inbox.hub_epoch,
        revision=revision,
        host_epoch=HOST_EPOCH,
        accepted=accepted or [],
        agents=agents or [],
        exits=exits or [],
    )
    size = node_reports.report_size(report)
    admitted = inbox.admit(machine_id, inbox.hub_epoch, revision, size, 10**9)
    assert admitted is node_reports.Admission.ADMITTED
    inbox.put(machine_id, report, size)


def _scripted_cancels(node: AsyncMock, *replies: Any) -> None:
    """Answers agent.cancel in order; a run whose cancel landed cannot spawn afterwards."""
    queued = list(replies)
    cancelled: set[str] = set()

    async def send(machine_id: str, command: str, payload: dict[str, Any]) -> Any:
        if command == "agent.spawn" and payload["run_id"] in cancelled:
            raise NodeCommandError("cancelled")
        if command != "agent.cancel":
            raise AssertionError(f"unexpected node command {command}")
        cancelled.add(payload["run_id"])
        reply = queued.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    node.send.side_effect = send


def _coordinator(
    temp_db: HubDatabase,
    task_manager: LocalTaskManager | None = None,
) -> SessionCoordinator:
    coordinator = SessionCoordinator(
        session_storage=cast(HookSessionManager, SessionManager(temp_db)),
        agent_run_manager=LocalAgentRunManager(temp_db),
        task_manager=task_manager,
    )
    coordinator.set_completion_registry(CompletionEventRegistry())
    return coordinator


def _handlers(temp_db: HubDatabase, coordinator: SessionCoordinator) -> EventHandlers:
    return EventHandlers(
        session_manager=cast(HookSessionManager, SessionManager(temp_db)),
        session_coordinator=coordinator,
        agent_run_manager=LocalAgentRunManager(temp_db),
        get_machine_id=lambda: "local-machine",
    )


def _health_monitor(temp_db: HubDatabase, coordinator: SessionCoordinator) -> AgentHealthMonitor:
    sessions = SessionManager(temp_db)
    return AgentHealthMonitor(
        agent_run_manager=LocalAgentRunManager(temp_db),
        db=temp_db,
        get_session_manager=lambda: sessions,
        stall_classifier=MagicMock(),
        cleanup_handler=MagicMock(),
        tmux_config=TmuxConfig(),
        get_session_coordinator=lambda: coordinator,
    )


def _hook(
    event_type: HookEventType,
    session_id: str,
    machine_id: str,
    *,
    source: SessionSource = SessionSource.CLAUDE,
    data: dict[str, Any] | None = None,
    disposition: TurnDisposition = "unknown",
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=source,
        data=data or {},
        metadata={"_platform_session_id": session_id},
        machine_id=machine_id,
        timestamp=datetime.now(UTC),
        turn_disposition=disposition,
    )


def _begin_turn(
    handlers: EventHandlers,
    session_id: str,
    machine_id: str,
    source: SessionSource = SessionSource.CLAUDE,
) -> None:
    handlers.handle_before_agent(
        _hook(HookEventType.BEFORE_AGENT, session_id, machine_id, source=source)
    )


def _stop(
    handlers: EventHandlers,
    session_id: str,
    machine_id: str,
    source: SessionSource,
    disposition: TurnDisposition,
) -> None:
    handlers.handle_stop(
        _hook(
            HookEventType.STOP,
            session_id,
            machine_id,
            source=source,
            disposition=disposition,
        )
    )


def _end_session(handlers: EventHandlers, session_id: str, machine_id: str) -> None:
    handlers.handle_session_end(
        _hook(HookEventType.SESSION_END, session_id, machine_id, data={"reason": "other"})
    )


@pytest.mark.asyncio
async def test_listed_pending_run_is_promoted(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
) -> None:
    run = _run(temp_db, sample_project["id"], remote_machine, status="pending")
    terminal_id = _pending(temp_db, sample_project["id"], remote_machine, run.id)
    _feed(remote_machine, 1, agents=[_agent(run.id, terminal_id)])

    await reconciler.reconcile()

    terminal = TerminalManager(temp_db).get(terminal_id)
    assert terminal is not None and terminal.state == "live"
    assert terminal.host_epoch == str(HOST_EPOCH)
    assert terminal.locator == {"host_terminal_id": _host(terminal_id)}
    refreshed = LocalAgentRunManager(temp_db).get(run.id)
    assert refreshed is not None
    assert refreshed.pid == 4321
    assert refreshed.terminal_id == terminal_id


@pytest.mark.asyncio
async def test_session_end_first_exits_the_listed_terminal(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
) -> None:
    run = _run(temp_db, sample_project["id"], remote_machine, status=STATUS_SUCCESS)
    terminal_id = _pending(temp_db, sample_project["id"], remote_machine, run.id)
    _feed(remote_machine, 1, agents=[_agent(run.id, terminal_id)])

    await reconciler.reconcile()

    assert _state(temp_db, terminal_id) == "exited"
    refreshed = LocalAgentRunManager(temp_db).get(run.id)
    assert refreshed is not None
    assert refreshed.status == STATUS_SUCCESS
    assert refreshed.pid is None
    assert refreshed.terminal_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("row_epoch", "listed", "settles_between", "cleaned"),
    [
        pytest.param(HOST_EPOCH, False, False, True, id="unlisted"),
        pytest.param(OLDER_EPOCH, True, False, True, id="older-epoch"),
        pytest.param(HOST_EPOCH, False, True, False, id="settles-between"),
    ],
)
async def test_absent_live_run_settles_after_two_reports(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
    cleanup: AsyncMock,
    row_epoch: int,
    listed: bool,
    settles_between: bool,
    cleaned: bool,
) -> None:
    run = _run(temp_db, sample_project["id"], remote_machine, status="running")
    terminal_id = _live(temp_db, sample_project["id"], remote_machine, run.id, host_epoch=row_epoch)
    agents = [_agent(run.id, terminal_id)] if listed else []

    _feed(remote_machine, 1, agents=agents)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "live"
    cleanup.assert_not_awaited()

    if settles_between:
        temp_db.execute("UPDATE agent_runs SET status = %s WHERE id = %s", (STATUS_SUCCESS, run.id))
    _feed(remote_machine, 2, agents=agents)
    await reconciler.reconcile()

    assert _state(temp_db, terminal_id) == "exited"
    if cleaned:
        cleanup.assert_awaited_once()
        assert _awaited(cleanup).args[0].id == run.id
    else:
        cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_exits_settle_by_launch(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
    cleanup: AsyncMock,
    node: AsyncMock,
) -> None:
    refused = _run(temp_db, sample_project["id"], remote_machine, status="pending")
    refused_terminal = _pending(temp_db, sample_project["id"], remote_machine, refused.id)
    _feed(
        remote_machine,
        1,
        exits=[_exit(refused.id, refused_terminal, launched=False, reason="launch_failed")],
    )
    await reconciler.reconcile()
    assert _state(temp_db, refused_terminal) == "exited"
    assert _awaited(cleanup).kwargs["terminal_payload"].startswith("remote spawn failed on")
    node.send.assert_not_awaited()

    launched = _run(temp_db, sample_project["id"], remote_machine, status="running")
    launched_terminal = _live(temp_db, sample_project["id"], remote_machine, launched.id)
    _feed(
        remote_machine,
        2,
        exits=[_exit(launched.id, launched_terminal, launched=True, reason="exited", exit_code=1)],
    )
    await reconciler.reconcile()
    assert _state(temp_db, launched_terminal) == "live"
    cleanup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "reason", "exited", "cleaned"),
    [
        pytest.param({"stopped": True}, DIED, True, True, id="stopped"),
        pytest.param(NodeCommandError("not_found"), DIED, True, True, id="not-found"),
        pytest.param(NodeCommandError("not_found"), None, True, False, id="not-found-completing"),
        pytest.param({"stopped": False}, DIED, False, False, id="not-stopped"),
        pytest.param(NodeCommandError("node_timeout"), DIED, False, False, id="node-timeout"),
    ],
)
async def test_in_doubt_spawn_is_fenced_before_settlement(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    cleanup: AsyncMock,
    node: AsyncMock,
    reply: Any,
    reason: str | None,
    exited: bool,
    cleaned: bool,
) -> None:
    dead_reason = AsyncMock(return_value=reason)
    reconciler = _reconciler(temp_db, cleanup, dead_reason=dead_reason)
    run = _run(temp_db, sample_project["id"], remote_machine, status="pending")
    terminal_id = _pending(temp_db, sample_project["id"], remote_machine, run.id)
    _age_pending(temp_db, terminal_id)
    node.send.side_effect = [reply]

    _feed(remote_machine, 1)
    await reconciler.reconcile()
    node.send.assert_not_awaited()

    _feed(remote_machine, 2)
    await reconciler.reconcile()
    node.send.assert_awaited_once_with(
        remote_machine,
        "agent.cancel",
        {"run_id": run.id, "terminal_id": terminal_id, "signal": "KILL", "close_terminal": True},
    )
    assert _state(temp_db, terminal_id) == ("exited" if exited else "pending")
    assert dead_reason.await_count == (1 if exited else 0)
    if cleaned:
        cleanup.assert_awaited_once()
        assert _awaited(cleanup).args[0].id == run.id
        assert _awaited(cleanup).kwargs["terminal_payload"] == reason
    else:
        cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_configured_deadline_governs_the_fence(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    cleanup: AsyncMock,
    node: AsyncMock,
) -> None:
    run = _run(temp_db, sample_project["id"], remote_machine, status="pending")
    terminal_id = _pending(temp_db, sample_project["id"], remote_machine, run.id)
    _age_pending(temp_db, terminal_id, timedelta(seconds=60))
    node.send.return_value = {"stopped": True}
    short = _reconciler(temp_db, cleanup, in_doubt_seconds=30.0)
    patient = _reconciler(temp_db, cleanup)

    _feed(remote_machine, 1)
    await short.reconcile()
    _feed(remote_machine, 2)
    await short.reconcile()
    assert _state(temp_db, terminal_id) == "exited"
    node.send.assert_awaited_once()

    other = _pending(temp_db, sample_project["id"], remote_machine, run.id)
    _age_pending(temp_db, other, timedelta(seconds=60))
    _feed(remote_machine, 3)
    await patient.reconcile()
    _feed(remote_machine, 4)
    await patient.reconcile()
    assert _state(temp_db, other) == "pending"
    node.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "agy"])
async def test_recovery_protected_runs_are_not_settled(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    cleanup: AsyncMock,
    node: AsyncMock,
    provider: str,
) -> None:
    dead_reason = AsyncMock(return_value=DIED)
    reconciler = _reconciler(temp_db, cleanup, dead_reason=dead_reason)
    protected = {"reconciliation_pending": True}
    absent = _run(
        temp_db,
        sample_project["id"],
        remote_machine,
        status="running",
        provider=provider,
        resume_metadata=protected,
    )
    absent_terminal = _live(temp_db, sample_project["id"], remote_machine, absent.id)
    doubtful = _run(
        temp_db,
        sample_project["id"],
        remote_machine,
        status="pending",
        provider=provider,
        resume_metadata=protected,
    )
    doubtful_terminal = _pending(temp_db, sample_project["id"], remote_machine, doubtful.id)
    _age_pending(temp_db, doubtful_terminal)
    refused = _run(
        temp_db,
        sample_project["id"],
        remote_machine,
        status="pending",
        provider=provider,
        resume_metadata=protected,
    )
    refused_terminal = _pending(temp_db, sample_project["id"], remote_machine, refused.id)
    exits = [_exit(refused.id, refused_terminal, launched=False, reason="launch_failed")]

    _feed(remote_machine, 1, exits=exits)
    await reconciler.reconcile()
    _feed(remote_machine, 2, exits=exits)
    await reconciler.reconcile()

    assert _state(temp_db, absent_terminal) == "live"
    assert _state(temp_db, doubtful_terminal) == "pending"
    assert _state(temp_db, refused_terminal) == "pending"
    node.send.assert_not_awaited()
    dead_reason.assert_not_awaited()
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreported_and_local_rows_untouched(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
    node: AsyncMock,
) -> None:
    reporting = _run(temp_db, sample_project["id"], remote_machine, status="running")
    reporting_terminal = _live(temp_db, sample_project["id"], remote_machine, reporting.id)
    silent_machine = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(silent_machine, TEST_USER_ID)
    silent = _run(temp_db, sample_project["id"], silent_machine, status="pending")
    silent_terminal = _pending(temp_db, sample_project["id"], silent_machine, silent.id)
    _age_pending(temp_db, silent_terminal)
    local_machine = require_machine_id()
    LocalMachineManager(temp_db).upsert_seen(local_machine, TEST_USER_ID)
    local = _run(temp_db, sample_project["id"], local_machine, status="running")
    local_terminal = _live(temp_db, sample_project["id"], local_machine, local.id)

    _feed(remote_machine, 1)
    await reconciler.reconcile()
    _feed(remote_machine, 2)
    await reconciler.reconcile()

    assert _state(temp_db, reporting_terminal) == "exited"
    node.send.assert_not_awaited()
    assert _state(temp_db, silent_terminal) == "pending"
    assert _state(temp_db, local_terminal) == "live"


@pytest.mark.asyncio
async def test_ended_run_terminal_still_settles(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    cleanup: AsyncMock,
    node: AsyncMock,
) -> None:
    # Spawn landed and its reply was lost. The run ended between reports; the test writes its
    # final status directly, so the forwarded session start and end are not exercised here.
    _scripted_cancels(
        node,
        {"stopped": False},
        NodeCommandError("node_timeout"),
        NodeCommandError("not_found"),
    )
    reconciler = _reconciler(temp_db, cleanup)
    run = _run(temp_db, sample_project["id"], remote_machine, status="running")
    terminal_id = _pending(temp_db, sample_project["id"], remote_machine, run.id)
    _age_pending(temp_db, terminal_id)
    temp_db.execute("UPDATE agent_runs SET status = %s WHERE id = %s", (STATUS_SUCCESS, run.id))
    silent_machine = str(uuid4())
    LocalMachineManager(temp_db).upsert_seen(silent_machine, TEST_USER_ID)
    silent = _run(temp_db, sample_project["id"], silent_machine, status="pending")
    silent_terminal = _pending(temp_db, sample_project["id"], silent_machine, silent.id)
    _age_pending(temp_db, silent_terminal)

    _feed(remote_machine, 1)
    await reconciler.reconcile()
    node.send.assert_not_awaited()

    _feed(remote_machine, 2)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "pending"

    _feed(remote_machine, 3)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "pending"

    _feed(remote_machine, 4)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "exited"
    assert node.send.await_count == 3
    stored = LocalAgentRunManager(temp_db).get(run.id)
    assert stored is not None and stored.status == STATUS_SUCCESS
    cleanup.assert_not_awaited()

    with pytest.raises(NodeCommandError) as refused:
        await node.send(
            remote_machine, "agent.spawn", {"run_id": run.id, "terminal_id": terminal_id}
        )
    assert refused.value.code == "cancelled"
    assert _state(temp_db, silent_terminal) == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "settles"),
    [
        pytest.param("none", True, id="exited-run-settles"),
        pytest.param("completes", False, id="run-completes"),
        pytest.param("gains-pending", False, id="gains-pending"),
        pytest.param("gains-live", False, id="gains-live"),
        pytest.param("no-terminal", False, id="no-terminal"),
    ],
)
async def test_run_left_active_after_terminal_exit_settles(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    cleanup: AsyncMock,
    change: str,
    settles: bool,
) -> None:
    reconciler = _reconciler(temp_db, cleanup)
    healthy = []
    for _ in range(100):
        healthy_run = _run(temp_db, sample_project["id"], remote_machine, status="running")
        healthy_terminal = _live(temp_db, sample_project["id"], remote_machine, healthy_run.id)
        healthy.append(_agent(healthy_run.id, healthy_terminal))
    run = _run(temp_db, sample_project["id"], remote_machine, status="running")
    child = _child_session(temp_db, sample_project["id"], remote_machine, run.id)
    if change != "no-terminal":
        terminal_id = _live(temp_db, sample_project["id"], remote_machine, run.id, child)
        TerminalManager(temp_db).mark_exited(terminal_id)

    _feed(remote_machine, 1, agents=healthy)
    await reconciler.reconcile()
    cleanup.assert_not_awaited()

    if change == "completes":
        temp_db.execute("UPDATE agent_runs SET status = %s WHERE id = %s", (STATUS_SUCCESS, run.id))
    elif change == "gains-pending":
        _pending(temp_db, sample_project["id"], remote_machine, run.id, child)
    elif change == "gains-live":
        _live(temp_db, sample_project["id"], remote_machine, run.id, child)
    _feed(remote_machine, 2, agents=healthy)
    await reconciler.reconcile()

    if settles:
        cleanup.assert_awaited_once()
        assert _awaited(cleanup).args[0].id == run.id
        assert _awaited(cleanup).kwargs["terminal_payload"] == (
            "remote terminal exited before its run settled"
        )
    else:
        cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("began", "disposition", "reason"),
    [
        pytest.param(True, "completed", None, id="completed"),
        pytest.param(
            True,
            "ended_non_user",
            "agent exited after its last turn ended without completing (ended_non_user)",
            id="ended-non-user",
        ),
        pytest.param(False, None, DIED, id="no-forwarded-turn"),
    ],
)
async def test_agy_exit_completes_from_forwarded_stop(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    cleanup: AsyncMock,
    began: bool,
    disposition: TurnDisposition | None,
    reason: str | None,
) -> None:
    coordinator = _coordinator(temp_db)
    handlers = _handlers(temp_db, coordinator)
    health = _health_monitor(temp_db, coordinator)
    reconciler = _reconciler(temp_db, cleanup, dead_reason=health._dead_terminal_reason)

    run = _run(temp_db, sample_project["id"], remote_machine, status="running", provider="agy")
    child = _child_session(temp_db, sample_project["id"], remote_machine, run.id, source="agy")
    terminal_id = _live(temp_db, sample_project["id"], remote_machine, run.id, child)
    if began:
        _begin_turn(handlers, child, remote_machine, SessionSource.AGY)
    if disposition is not None:
        _stop(handlers, child, remote_machine, SessionSource.AGY, disposition)

    _feed(
        remote_machine,
        1,
        exits=[_exit(run.id, terminal_id, launched=True, reason="exited", exit_code=0)],
    )
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "live"
    cleanup.assert_not_awaited()

    _feed(remote_machine, 2)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "exited"
    if reason is None:
        cleanup.assert_not_awaited()
        completed = LocalAgentRunManager(temp_db).get(run.id)
        assert completed is not None and completed.status == STATUS_SUCCESS
    else:
        cleanup.assert_awaited_once()
        assert _awaited(cleanup).args[0].id == run.id
        assert _awaited(cleanup).kwargs["terminal_payload"] == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("begin", "closed", "status"),
    [
        pytest.param(True, False, STATUS_SUCCESS, id="begun-turn"),
        pytest.param(False, False, STATUS_ERROR, id="no-begun-turn"),
        pytest.param(False, True, STATUS_SUCCESS, id="closed-task"),
    ],
)
async def test_node_session_end_counts_forwarded_turns(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    begin: bool,
    closed: bool,
    status: str,
) -> None:
    task_manager = LocalTaskManager(temp_db)
    coordinator = _coordinator(temp_db, task_manager)
    handlers = _handlers(temp_db, coordinator)
    task_id = None
    if closed:
        task = task_manager.create_task(
            project_id=sample_project["id"],
            title="Closed by the node",
            validation_criteria="The node's run closes the task.",
        )
        task_manager.close_task(task.id, reason="done", closed_commit_sha="abc123")
        task_id = task.id
    run = _run(temp_db, sample_project["id"], remote_machine, status="running", task_id=task_id)
    session = _child_session(temp_db, sample_project["id"], remote_machine, run.id)
    if begin:
        _begin_turn(handlers, session, remote_machine)

    _end_session(handlers, session, remote_machine)

    settled = LocalAgentRunManager(temp_db).get(run.id)
    assert settled is not None
    assert settled.status == status
    if status == STATUS_ERROR:
        assert NO_ACTIVITY in (settled.error or "")


@pytest.mark.asyncio
async def test_accepted_spawn_is_never_fenced(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
    cleanup: AsyncMock,
    node: AsyncMock,
) -> None:
    run = _run(temp_db, sample_project["id"], remote_machine, status="pending")
    terminal_id = _pending(temp_db, sample_project["id"], remote_machine, run.id)
    _age_pending(temp_db, terminal_id)

    _feed(remote_machine, 1, accepted=[run.id])
    await reconciler.reconcile()
    _feed(remote_machine, 2, accepted=[run.id])
    await reconciler.reconcile()

    node.send.assert_not_awaited()
    assert _state(temp_db, terminal_id) == "pending"
    cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_exit_waits_for_a_second_report(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
    cleanup: AsyncMock,
) -> None:
    run = _run(temp_db, sample_project["id"], remote_machine, status="running")
    terminal_id = _live(temp_db, sample_project["id"], remote_machine, run.id)
    _feed(remote_machine, 1, exits=[_exit(run.id, terminal_id, launched=True, reason="cancelled")])
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "live"
    cleanup.assert_not_awaited()

    _feed(remote_machine, 2)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "exited"
    cleanup.assert_awaited_once()


def test_configured_deadline_reaches_both_reconcilers(temp_db: HubDatabase) -> None:
    monitor = AgentLifecycleMonitor(
        agent_run_manager=LocalAgentRunManager(temp_db),
        db=temp_db,
        detection_registry=DETECTION_REGISTRY,
        terminal_services=_fake_terminal_services(temp_db, LifecycleRuntime(backend="native")),
        terminal_config=TerminalConfig(spawn_in_doubt_seconds=30.0),
    )

    assert monitor._reconciliation._spawn_in_doubt_seconds == 30.0
    assert monitor._health_monitor._remote_reconciler._in_doubt_seconds == 30.0
