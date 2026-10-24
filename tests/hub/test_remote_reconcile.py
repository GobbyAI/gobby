"""The hub settles remote runs and terminals from the node's reports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from gobby.hooks.session_types import HookSessionManager
from gobby.hub import node_reports
from gobby.hub.node_client import NodeCommandError
from gobby.hub.node_commands import ReportAgent, ReportBody, ReportExit
from gobby.hub.remote_reconcile import RemoteRunReconciler
from gobby.storage.agents import STATUS_ERROR, STATUS_SUCCESS, LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.machines import LocalMachineManager
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.utils.machine_id import require_machine_id
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit

HOST_EPOCH = 7
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
    dead_reason: AsyncMock | None = None,
) -> RemoteRunReconciler:
    async def run_db(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    return RemoteRunReconciler(
        run_manager=LocalAgentRunManager(temp_db),
        terminals=TerminalManager(temp_db),
        cleanup_agent=cleanup,
        dead_reason=dead_reason or AsyncMock(return_value="terminal session died unexpectedly"),
        run_db=run_db,
    )


@pytest.fixture
def reconciler(temp_db: HubDatabase, cleanup: AsyncMock) -> RemoteRunReconciler:
    return _reconciler(temp_db, cleanup)


def _run(temp_db: HubDatabase, project_id: str, machine_id: str, *, status: str) -> Any:
    runs = LocalAgentRunManager(temp_db)
    parent = str(uuid4())
    temp_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, status)"
        " VALUES (%s, %s, %s, 'claude', %s, 'active')",
        (parent, f"parent-{parent}", machine_id, project_id),
    )
    run = runs.create(
        parent_session_id=parent,
        provider="claude",
        prompt="do it",
        machine_id=machine_id,
    )
    temp_db.execute("UPDATE agent_runs SET status = %s WHERE id = %s", (status, run.id))
    refreshed = runs.get(run.id)
    assert refreshed is not None
    return refreshed


def _child_session(temp_db: HubDatabase, project_id: str, machine_id: str, run_id: str) -> str:
    session_id = str(uuid4())
    temp_db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, status,"
        " agent_run_id) VALUES (%s, %s, %s, 'claude', %s, 'active', %s)",
        (session_id, f"child-{session_id}", machine_id, project_id, run_id),
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
) -> str:
    terminal_id = _pending(temp_db, project_id, machine_id, run_id, session_id)
    host = _host(terminal_id)
    TerminalManager(temp_db).promote_to_live(
        terminal_id,
        locator={"host_terminal_id": host},
        locator_key=native_locator_key(str(HOST_EPOCH), host),
        host_epoch=str(HOST_EPOCH),
    )
    return terminal_id


def _age_pending(temp_db: HubDatabase, terminal_id: str) -> None:
    temp_db.execute(
        "UPDATE terminals SET attempt_started_at = attempt_started_at - %s WHERE id = %s",
        (timedelta(hours=1), terminal_id),
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
    refreshed = LocalAgentRunManager(temp_db).get(run.id)
    assert refreshed is not None
    assert refreshed.pid == 4321
    assert refreshed.terminal_id == terminal_id


async def test_absent_live_run_settles_after_two_reports(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
    cleanup: AsyncMock,
) -> None:
    run = _run(temp_db, sample_project["id"], remote_machine, status="running")
    terminal_id = _live(temp_db, sample_project["id"], remote_machine, run.id)

    _feed(remote_machine, 1)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "live"
    cleanup.assert_not_awaited()

    _feed(remote_machine, 2)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "exited"
    cleanup.assert_awaited_once()
    assert _awaited(cleanup).args[0].id == run.id


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


async def test_in_doubt_spawn_is_fenced_before_settlement(
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
    node.send.return_value = {"stopped": True}

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
    assert _state(temp_db, terminal_id) == "exited"
    cleanup.assert_awaited_once()


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


async def test_ended_run_terminal_still_settles(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
    cleanup: AsyncMock,
    node: AsyncMock,
) -> None:
    run = _run(temp_db, sample_project["id"], remote_machine, status=STATUS_SUCCESS)
    terminal_id = _pending(temp_db, sample_project["id"], remote_machine, run.id)
    _age_pending(temp_db, terminal_id)
    node.send.return_value = {"stopped": False}

    _feed(remote_machine, 1)
    await reconciler.reconcile()
    _feed(remote_machine, 2)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "pending"
    cleanup.assert_not_awaited()

    node.send.return_value = {"stopped": True}
    _feed(remote_machine, 3)
    await reconciler.reconcile()
    _feed(remote_machine, 4)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "exited"
    cleanup.assert_not_awaited()


async def test_run_left_active_after_terminal_exit_settles(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    reconciler: RemoteRunReconciler,
    cleanup: AsyncMock,
) -> None:
    healthy = []
    for _ in range(100):
        healthy_run = _run(temp_db, sample_project["id"], remote_machine, status="running")
        healthy_terminal = _live(temp_db, sample_project["id"], remote_machine, healthy_run.id)
        healthy.append(_agent(healthy_run.id, healthy_terminal))
    run = _run(temp_db, sample_project["id"], remote_machine, status="running")
    child = _child_session(temp_db, sample_project["id"], remote_machine, run.id)
    terminal_id = _live(temp_db, sample_project["id"], remote_machine, run.id, child)
    TerminalManager(temp_db).mark_exited(terminal_id)

    _feed(remote_machine, 1, agents=healthy)
    await reconciler.reconcile()
    cleanup.assert_not_awaited()

    _feed(remote_machine, 2, agents=healthy)
    await reconciler.reconcile()
    cleanup.assert_awaited_once()
    assert _awaited(cleanup).args[0].id == run.id
    assert _awaited(cleanup).kwargs["terminal_payload"] == (
        "remote terminal exited before its run settled"
    )


async def test_agy_exit_completes_from_forwarded_stop(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
    cleanup: AsyncMock,
) -> None:
    dead_reason = AsyncMock(return_value=None)
    reconciler = _reconciler(temp_db, cleanup, dead_reason=dead_reason)

    run = _run(temp_db, sample_project["id"], remote_machine, status="running")
    temp_db.execute("UPDATE agent_runs SET provider = 'agy' WHERE id = %s", (run.id,))
    terminal_id = _live(temp_db, sample_project["id"], remote_machine, run.id)
    _feed(
        remote_machine,
        1,
        exits=[_exit(run.id, terminal_id, launched=True, reason="exited", exit_code=0)],
    )
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "live"
    dead_reason.assert_not_awaited()

    _feed(remote_machine, 2)
    await reconciler.reconcile()
    assert _state(temp_db, terminal_id) == "exited"
    dead_reason.assert_awaited_once()
    assert _awaited(dead_reason).args[0].id == run.id
    cleanup.assert_not_awaited()


async def test_node_session_end_counts_forwarded_turns(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    remote_machine: str,
) -> None:
    from gobby.hooks.event_handlers import EventHandlers
    from gobby.hooks.events import HookEvent, HookEventType, SessionSource
    from gobby.hooks.session_coordinator import SessionCoordinator
    from gobby.storage.sessions import SessionManager

    runs = LocalAgentRunManager(temp_db)
    sessions = cast(HookSessionManager, SessionManager(temp_db))
    coordinator = SessionCoordinator(session_storage=sessions, agent_run_manager=runs)
    handlers = EventHandlers(
        session_manager=sessions,
        session_coordinator=coordinator,
        agent_run_manager=runs,
        get_machine_id=lambda: "local-machine",
    )

    def _node_session(run_id: str) -> str:
        session_id = str(uuid4())
        temp_db.execute(
            "INSERT INTO sessions (id, external_id, machine_id, source, project_id, status,"
            " agent_run_id) VALUES (%s, %s, %s, 'claude', %s, 'active', %s)",
            (session_id, f"node-{session_id}", remote_machine, sample_project["id"], run_id),
        )
        return session_id

    begun = _run(temp_db, sample_project["id"], remote_machine, status="running")
    begun_session = _node_session(begun.id)
    handlers.handle_before_agent(
        HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=begun_session,
            source=SessionSource.CLAUDE,
            data={},
            metadata={"_platform_session_id": begun_session},
            machine_id=remote_machine,
            timestamp=datetime.now(UTC),
        )
    )
    handlers.handle_session_end(
        HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id=begun_session,
            source=SessionSource.CLAUDE,
            data={"reason": "other"},
            metadata={"_platform_session_id": begun_session},
            machine_id=remote_machine,
            timestamp=datetime.now(UTC),
        )
    )
    completed = runs.get(begun.id)
    assert completed is not None and completed.status == STATUS_SUCCESS

    silent = _run(temp_db, sample_project["id"], remote_machine, status="running")
    silent_session = _node_session(silent.id)
    handlers.handle_session_end(
        HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id=silent_session,
            source=SessionSource.CLAUDE,
            data={"reason": "other"},
            metadata={"_platform_session_id": silent_session},
            machine_id=remote_machine,
            timestamp=datetime.now(UTC),
        )
    )
    failed = runs.get(silent.id)
    assert failed is not None and failed.status == STATUS_ERROR


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


async def test_unanswered_fence_leaves_spawn_pending(
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
    node.send.side_effect = NodeCommandError("node_timeout")

    _feed(remote_machine, 1)
    await reconciler.reconcile()
    _feed(remote_machine, 2)
    await reconciler.reconcile()

    node.send.assert_awaited_once()
    assert _state(temp_db, terminal_id) == "pending"
    cleanup.assert_not_awaited()


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
