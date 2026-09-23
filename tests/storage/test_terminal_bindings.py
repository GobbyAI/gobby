"""Acceptance 3.2.2: CLI sessions bind to the gobby-owned terminal they run in."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import psutil
import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import Terminal, TerminalManager, native_locator_key
from tests.storage.test_terminals import LOCAL_MACHINE_ID

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


def _live_cli() -> dict[str, object]:
    return {"parent_pid": os.getpid(), "parent_create_time": psutil.Process().create_time()}


def _dead_cli() -> dict[str, object]:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return {"parent_pid": child.pid, "parent_create_time": time.time()}


def _session(
    sessions: SessionManager,
    project_id: str,
    terminal_context: dict[str, object],
    *,
    session_type: str = "terminal",
) -> Session:
    return sessions.register(
        external_id=f"bind-{uuid.uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
        terminal_context=terminal_context,
        session_type=session_type,
    )


def _native_row(
    terminals: TerminalManager,
    project_id: str,
    *,
    session_id: str | None = None,
    agent_run_id: str | None = None,
) -> Terminal:
    terminal_id = str(uuid.uuid4())
    return terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
        machine_id=LOCAL_MACHINE_ID,
        session_id=session_id,
        agent_run_id=agent_run_id,
    )


def _bound_session_id(terminals: TerminalManager, terminal_id: str) -> str | None:
    row = terminals.get(terminal_id)
    assert row is not None
    return row.session_id


def test_bind_session_guards(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    project_manager: LocalProjectManager,
) -> None:
    terminals = TerminalManager(temp_db)
    project_id = sample_project["id"]

    pane = _native_row(terminals, project_id)
    first = _session(session_manager, project_id, _live_cli())
    bound = terminals.bind_session(pane.id, first.id, project_id)
    assert bound is not None
    assert bound.session_id == first.id

    second = _session(session_manager, project_id, _live_cli())
    assert terminals.bind_session(pane.id, second.id, project_id) is None
    assert _bound_session_id(terminals, pane.id) == first.id

    session_manager.update_status(first.id, "expired")
    rebound = terminals.bind_session(pane.id, second.id, project_id)
    assert rebound is not None
    assert rebound.session_id == second.id

    crashed_pane = _native_row(terminals, project_id)
    crashed = _session(session_manager, project_id, _dead_cli())
    assert terminals.bind_session(crashed_pane.id, crashed.id, project_id) is not None
    successor = _session(session_manager, project_id, _live_cli())
    taken = terminals.bind_session(crashed_pane.id, successor.id, project_id)
    assert taken is not None
    assert taken.session_id == successor.id

    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=first.id,
        provider="claude",
        prompt="agent terminals are bound at spawn",
    )
    agent_pane = _native_row(terminals, project_id, agent_run_id=run.id)
    interactive = _session(session_manager, project_id, _live_cli())
    assert terminals.bind_session(agent_pane.id, interactive.id, project_id) is None
    assert _bound_session_id(terminals, agent_pane.id) is None

    other_project = project_manager.create(name="bind-other-project")
    foreign_pane = _native_row(terminals, other_project.id)
    assert terminals.bind_session(foreign_pane.id, interactive.id, project_id) is None
    assert _bound_session_id(terminals, foreign_pane.id) is None

    web_pane = _native_row(terminals, project_id)
    web_chat = _session(session_manager, project_id, _live_cli(), session_type="web_chat")
    assert terminals.bind_session(web_pane.id, web_chat.id, project_id) is None
    assert _bound_session_id(terminals, web_pane.id) is None

    # GOBBY_TERMINAL_ID arrives from the pane environment, which a user can overwrite.
    assert terminals.bind_session("not-a-terminal-id", interactive.id, project_id) is None


def test_resolve_live_for_session_context_id_guard_matrix(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
    project_manager: LocalProjectManager,
) -> None:
    terminals = TerminalManager(temp_db)
    project_id = sample_project["id"]
    session = _session(session_manager, project_id, {})

    bound = _native_row(terminals, project_id)
    assert terminals.bind_session(bound.id, session.id, project_id) is not None
    resolved = terminals.resolve_live_for_session(session)
    assert resolved is not None
    assert resolved.id == bound.id
    assert terminals.release_session(bound.id, session.id) is not None

    session.terminal_context = {"gobby_terminal_id": bound.id}
    resolved = terminals.resolve_live_for_session(session)
    assert resolved is not None
    assert resolved.id == bound.id

    session.terminal_context = {"gobby_terminal_id": "not-a-uuid"}
    assert terminals.resolve_live_for_session(session) is None

    foreign_project = project_manager.create(name="resolver-foreign-project")
    foreign = _native_row(terminals, foreign_project.id)
    session.terminal_context = {"gobby_terminal_id": foreign.id}
    assert terminals.resolve_live_for_session(session) is None

    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id,
        provider="claude",
        prompt="resolver must not take agent terminals",
    )
    agent = _native_row(terminals, project_id, agent_run_id=run.id)
    session.terminal_context = {"gobby_terminal_id": agent.id}
    assert terminals.resolve_live_for_session(session) is None

    exited = _native_row(terminals, project_id)
    assert terminals.transition_for_test(exited.id, "pending", "exited") is not None
    session.terminal_context = {"gobby_terminal_id": exited.id}
    assert terminals.resolve_live_for_session(session) is None

    other = _session(session_manager, project_id, _live_cli())
    held = _native_row(terminals, project_id)
    assert terminals.bind_session(held.id, other.id, project_id) is not None
    session.terminal_context = {"gobby_terminal_id": held.id}
    assert terminals.resolve_live_for_session(session) is None


def test_rebind_releases_only_stale_non_agent_owner_rows(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    terminals = TerminalManager(temp_db)
    project_id = sample_project["id"]
    session = _session(session_manager, project_id, _live_cli())

    stale = _native_row(terminals, project_id, session_id=session.id)
    orphaned = _native_row(terminals, project_id, session_id=session.id)
    pending = _native_row(terminals, project_id, session_id=session.id)
    live = _native_row(terminals, project_id, session_id=session.id)
    assert terminals.transition_for_test(stale.id, "pending", "exited") is not None
    host_epoch = "cleanup-test-epoch"
    host_terminal_id = "cleanup-test-host"
    assert (
        terminals.promote_to_live(
            orphaned.id,
            locator={"host_terminal_id": host_terminal_id},
            locator_key=native_locator_key(host_epoch, host_terminal_id),
            host_epoch=host_epoch,
        )
        is not None
    )
    assert terminals.transition_for_test(orphaned.id, "live", "orphaned") is not None
    live_host_epoch = "cleanup-live-epoch"
    live_host_terminal_id = "cleanup-live-host"
    assert (
        terminals.promote_to_live(
            live.id,
            locator={"host_terminal_id": live_host_terminal_id},
            locator_key=native_locator_key(live_host_epoch, live_host_terminal_id),
            host_epoch=live_host_epoch,
        )
        is not None
    )

    run = LocalAgentRunManager(temp_db).create(
        parent_session_id=session.id,
        provider="claude",
        prompt="preserve agent terminal history",
    )
    agent = _native_row(
        terminals,
        project_id,
        session_id=session.id,
        agent_run_id=run.id,
    )
    assert terminals.transition_for_test(agent.id, "pending", "exited") is not None
    external_host_epoch = "cleanup-external-epoch"
    external_host_terminal_id = "cleanup-external-host"
    external = terminals.upsert_external(
        project_id=project_id,
        backend="native",
        locator={"host_terminal_id": external_host_terminal_id},
        locator_key=native_locator_key(external_host_epoch, external_host_terminal_id),
        host_epoch=external_host_epoch,
        session_id=session.id,
    )
    assert terminals.mark_exited(external.id) is not None

    target = _native_row(terminals, project_id)
    assert terminals.bind_session(target.id, session.id, project_id) is not None

    assert _bound_session_id(terminals, stale.id) is None
    assert _bound_session_id(terminals, orphaned.id) is None
    assert _bound_session_id(terminals, pending.id) == session.id
    assert _bound_session_id(terminals, live.id) == session.id
    assert _bound_session_id(terminals, agent.id) == session.id
    assert _bound_session_id(terminals, external.id) == session.id
