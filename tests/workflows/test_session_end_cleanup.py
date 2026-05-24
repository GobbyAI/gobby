"""Regression tests for SESSION_END workflow instance cleanup."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers._session_end import SessionEndMixin
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    database.execute(
        "INSERT INTO projects (id, name) VALUES (?, ?)",
        ("proj1", "test-project"),
    )
    yield database


def _ensure_session(db: HubDatabase, session_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (session_id, f"ext-{session_id}", "machine-1", "claude", "proj1"),
    )


def _make_event(session_id: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.SESSION_END,
        session_id=f"ext-{session_id}",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(),
        data={},
        metadata={"_platform_session_id": session_id},
    )


class _SessionEndHandler(SessionEndMixin):
    def __init__(self, db: HubDatabase) -> None:
        self.logger = MagicMock()
        self._session_manager = None
        self._workflow_handler = SimpleNamespace(rule_engine=RuleEngine(db=db))
        self._session_storage = MagicMock()
        self._session_storage.get.return_value = SimpleNamespace(
            created_at="2024-01-01T00:00:00Z",
            agent_run_id=None,
            status="active",
        )
        self._session_coordinator = None
        self._message_processor = None
        self._task_manager = None
        self._worktree_manager = None
        self._skill_manager = None
        self._skills_config = None
        self._session_task_manager = None
        self._dispatch_session_summaries_fn = None
        self._call_tool = None
        self._get_machine_id = MagicMock(return_value="machine-1")
        self._resolve_project_id = MagicMock(return_value="proj1")
        self._handler_map = {}


def _save_instance(
    manager: WorkflowInstanceManager,
    *,
    instance_id: str,
    session_id: str,
    workflow_name: str,
) -> None:
    manager.save_instance(
        WorkflowInstance(
            id=instance_id,
            session_id=session_id,
            workflow_name=workflow_name,
            current_step="terminate",
        )
    )


def test_session_end_deletes_workflow_instances_for_ending_session(db: HubDatabase) -> None:
    _ensure_session(db, "s1")
    instance_manager = WorkflowInstanceManager(db)
    _save_instance(
        instance_manager,
        instance_id="inst-1",
        session_id="s1",
        workflow_name="plan-adversary-steps",
    )

    handler = _SessionEndHandler(db)

    with patch("gobby.agents.tmux.get_tmux_pane_monitor", return_value=None):
        response = handler.handle_session_end(_make_event("s1"))

    assert response.decision == "allow"
    assert instance_manager.get_active_instances("s1") == []


def test_session_end_only_deletes_instances_for_target_session(db: HubDatabase) -> None:
    _ensure_session(db, "s1")
    _ensure_session(db, "s2")
    instance_manager = WorkflowInstanceManager(db)
    _save_instance(
        instance_manager,
        instance_id="inst-1",
        session_id="s1",
        workflow_name="plan-adversary-steps",
    )
    _save_instance(
        instance_manager,
        instance_id="inst-2",
        session_id="s2",
        workflow_name="developer",
    )

    handler = _SessionEndHandler(db)

    with patch("gobby.agents.tmux.get_tmux_pane_monitor", return_value=None):
        response = handler.handle_session_end(_make_event("s1"))

    assert response.decision == "allow"
    assert instance_manager.get_active_instances("s1") == []

    remaining = instance_manager.get_active_instances("s2")
    assert len(remaining) == 1
    assert remaining[0].workflow_name == "developer"
