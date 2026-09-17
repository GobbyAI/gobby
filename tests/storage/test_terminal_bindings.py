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
from gobby.storage.terminals import Terminal, TerminalManager
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
    terminals: TerminalManager, project_id: str, *, agent_run_id: str | None = None
) -> Terminal:
    terminal_id = str(uuid.uuid4())
    return terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
        machine_id=LOCAL_MACHINE_ID,
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
