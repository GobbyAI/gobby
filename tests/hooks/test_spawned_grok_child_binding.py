"""Spawned Grok child hooks bind to the spawned session on a native terminal (#22856).

Spawn exports ``GOBBY_SESSION_ID``; Grok runs child conversations inside its own
process, so their hooks carry the spawned session as an inherited platform hint.
The hint is accepted only for the same live process, with no tmux identity.
"""

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.session_lookup import SessionLookupService
from gobby.hooks.session_types import HookSessionManager
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.integration

_NATIVE_TTY = "/dev/ttys900"


def _service(session_manager: SessionManager, project_id: str) -> SessionLookupService:
    session_task_manager = MagicMock()
    session_task_manager.get_session_tasks.return_value = []
    return SessionLookupService(
        # hook_manager.py casts at this same boundary: SessionManager serves the
        # HookSessionManager protocol at runtime without nominally declaring it.
        session_manager=cast(HookSessionManager, session_manager),
        session_coordinator=MagicMock(),
        session_task_manager=session_task_manager,
        resolve_project_id=MagicMock(return_value=project_id),
        logger=MagicMock(),
    )


def _grok_hook(
    event_type: HookEventType,
    *,
    external_id: str,
    platform_session_id: str,
    parent_pid: int,
    project_id: str,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=external_id,
        source=SessionSource.GROK,
        timestamp=datetime.now(UTC),
        machine_id=require_machine_id(),
        project_id=project_id,
        data={"terminal_context": {"tty": _NATIVE_TTY, "parent_pid": parent_pid}},
        metadata={"_platform_session_id": platform_session_id},
    )


def _spawned_grok(session_manager: SessionManager, project_id: str) -> Session:
    """Create the row the way terminal spawn does: depth 1, then the run link."""
    parent = session_manager.register(
        external_id=f"operator-{uuid.uuid4()}",
        machine_id=require_machine_id(),
        source="claude",
        project_id=project_id,
    )
    spawned = session_manager.register(
        external_id=f"grok-{uuid.uuid4()}",
        machine_id=require_machine_id(),
        source="grok",
        project_id=project_id,
        parent_session_id=parent.id,
        agent_depth=1,
    )
    run = LocalAgentRunManager(session_manager.db).create(
        parent_session_id=parent.id,
        provider="grok",
        prompt="spawned grok",
        child_session_id=spawned.id,
    )
    linked = session_manager.update_terminal_pickup_metadata(spawned.id, agent_run_id=run.id)
    assert linked is not None
    assert linked.agent_run_id is not None
    return linked


def _resolve_own_hook(service: SessionLookupService, spawned: Session, project_id: str) -> None:
    """The spawned Grok's own hook records its live process on the row."""
    own = _grok_hook(
        HookEventType.BEFORE_AGENT,
        external_id=spawned.external_id,
        platform_session_id=spawned.id,
        parent_pid=os.getpid(),
        project_id=project_id,
    )
    assert service.resolve(own) == spawned.id
    assert "_native_subagent_binding" not in own.metadata


def test_spawned_grok_child_hook_binds_to_spawned_session(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    service = _service(session_manager, project_id)
    spawned = _spawned_grok(session_manager, project_id)
    _resolve_own_hook(service, spawned, project_id)

    recorded = session_manager.get(spawned.id)
    assert recorded is not None
    assert recorded.terminal_context is not None
    assert recorded.terminal_context["parent_pid"] == os.getpid()
    assert recorded.terminal_context["parent_create_time"] > 0

    child = _grok_hook(
        HookEventType.BEFORE_TOOL,
        external_id=f"grok-child-{uuid.uuid4()}",
        platform_session_id=spawned.id,
        parent_pid=os.getpid(),
        project_id=project_id,
    )

    assert service.resolve(child) == spawned.id
    assert child.metadata["_native_subagent_binding"] is True


def test_spawned_grok_hint_from_another_process_is_not_bound(
    session_manager: SessionManager, sample_project: dict[str, Any]
) -> None:
    project_id = sample_project["id"]
    service = _service(session_manager, project_id)
    spawned = _spawned_grok(session_manager, project_id)
    _resolve_own_hook(service, spawned, project_id)

    other = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        stranger = _grok_hook(
            HookEventType.BEFORE_TOOL,
            external_id=f"grok-stranger-{uuid.uuid4()}",
            platform_session_id=spawned.id,
            parent_pid=other.pid,
            project_id=project_id,
        )

        resolved = service.resolve(stranger)
    finally:
        other.terminate()
        other.wait(timeout=10)

    assert resolved is not None
    assert resolved != spawned.id
    assert "_native_subagent_binding" not in stranger.metadata
