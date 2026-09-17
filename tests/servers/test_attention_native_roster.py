"""Acceptance 3.2.4: a CLI bound to its native pane is on the attention roster."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.agents.prompt_detector import PromptDetector
from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.events import HookEventType
from gobby.hooks.session_types import HookSessionManager
from gobby.servers.http import HTTPServer
from gobby.servers.routes.attention import create_attention_router
from gobby.storage.attention import AttentionStateManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.terminals import TerminalManager, native_locator_key
from gobby.utils.machine_id import require_machine_id
from tests.agents.detection_test_support import BundledDetectionRegistry
from tests.hooks._event_handler_helpers import make_event

pytestmark = pytest.mark.unit


async def _run_db(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return function(*args, **kwargs)


def _roster_entries(
    temp_db: HubDatabase, session_manager: SessionManager, terminals: TerminalManager
) -> dict[str, dict[str, Any]]:
    config = SimpleNamespace(tmux=SimpleNamespace(socket_path="/tmp/gobby.sock"))
    server = SimpleNamespace(
        services=SimpleNamespace(
            attention_manager=AttentionStateManager(temp_db, epoch="native-roster"),
            agent_lifecycle_monitor=SimpleNamespace(
                prompt_detector=PromptDetector(BundledDetectionRegistry(), "claude")
            ),
            session_manager=session_manager,
            task_manager=SimpleNamespace(get_task=lambda _task_id: None),
            terminal_manager=terminals,
            agent_runner=None,
            database=temp_db,
            config=config,
            config_runtime=SimpleNamespace(snapshot=SimpleNamespace(active=config)),
            run_db=_run_db,
        )
    )
    app = FastAPI()
    app.include_router(create_attention_router(cast(HTTPServer, server)))
    with TestClient(app) as client:
        response = client.get("/api/attention/roster")
    assert response.status_code == 200
    return {entry["entry_id"]: entry for entry in response.json()["entries"]}


def test_bound_native_session_is_on_the_roster(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    session_manager: SessionManager,
) -> None:
    project_id = sample_project["id"]
    terminals = TerminalManager(temp_db)
    terminal_id = str(uuid.uuid4())
    terminals.create_pending(
        terminal_id=terminal_id,
        project_id=project_id,
        backend="native",
        ownership="gobby",
        spawn_key=terminal_id,
    )
    host_epoch = str(uuid.uuid4())
    host_terminal_id = str(uuid.uuid4())
    pane = terminals.promote_to_live(
        terminal_id,
        locator={"host_terminal_id": host_terminal_id},
        locator_key=native_locator_key(host_epoch, host_terminal_id),
        host_epoch=host_epoch,
    )
    assert pane is not None
    terminal_context = {
        "gobby_terminal_id": terminal_id,
        "parent_pid": os.getpid(),
        "parent_create_time": psutil.Process().create_time(),
    }
    session = session_manager.register(
        external_id=f"native-roster-{uuid.uuid4()}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
        terminal_context=terminal_context,
    )
    assert terminals.bind_session(terminal_id, session.id, project_id) is not None

    entry = _roster_entries(temp_db, session_manager, terminals)[f"session:{session.id}"]
    assert entry["session_id"] == session.id
    assert entry["terminal"]["terminal_id"] == terminal_id
    assert entry["terminal"]["backend"] == "native"
    assert entry["terminal"]["state"] == "live"

    handlers = EventHandlers(
        session_manager=cast(HookSessionManager, session_manager), terminal_manager=terminals
    )
    handlers.handle_session_end(
        make_event(
            HookEventType.SESSION_END,
            session_id=session.external_id,
            data={"reason": "prompt_input_exit"},
            metadata={"_platform_session_id": session.id},
        )
    )

    released = terminals.get(terminal_id)
    assert released is not None
    assert released.state == "live"
    assert released.session_id is None
    assert f"session:{session.id}" not in _roster_entries(temp_db, session_manager, terminals)
