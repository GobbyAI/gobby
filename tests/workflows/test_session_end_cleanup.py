"""Regression tests for SESSION_END workflow instance cleanup."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers._session_end import SessionEndMixin
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit

# Session/project/instance id columns are native uuid in PostgreSQL; synthetic
# ids like S1 would fail with `invalid input syntax for type uuid`.
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def db(temp_db: HubDatabase) -> Iterator[HubDatabase]:
    database = temp_db
    database.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "test-project"),
    )
    yield database


def _ensure_session(db: HubDatabase, session_id: str) -> None:
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (
            session_id,
            f"ext-{session_id}",
            "21000000-0000-4000-8000-000000000001",
            "claude",
            PROJECT_ID,
        ),
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
        self._workflow_handler = cast(Any, SimpleNamespace(rule_engine=RuleEngine(db=db)))
        self._session_storage = MagicMock()
        self._session_storage.get.return_value = SimpleNamespace(
            created_at="2024-01-01T00:00:00Z",
            agent_run_id=None,
            status="active",
        )
        self._session_coordinator = None
        self._session_end_auto_link_worker = None
        self._message_processor = None
        self._session_message_processors: dict[str, Any] = {}
        self._task_manager = None
        self._worktree_manager = None
        self._skill_manager = None
        self._skills_config = None
        self._session_task_manager = None
        self._dispatch_session_summaries_fn = None
        self._call_tool = None
        self._get_machine_id = MagicMock(return_value="21000000-0000-4000-8000-000000000001")
        self._resolve_project_id = MagicMock(return_value=PROJECT_ID)
        self._handler_map = {}


def _save(
    manager: AgentStepInstanceManager,
    *,
    session_id: str,
    workflow_name: str,
) -> None:
    manager.save(
        make_step_instance(
            session_id,
            agent_name=workflow_name.removesuffix("-steps"),
            current_step="terminate",
        )
    )


def test_session_end_deletes_workflow_instances_for_ending_session(db: HubDatabase) -> None:
    _ensure_session(db, S1)
    instance_manager = AgentStepInstanceManager(db)
    _save(
        instance_manager,
        session_id=S1,
        workflow_name="plan-adversary-steps",
    )

    handler = _SessionEndHandler(db)

    with patch("gobby.agents.tmux.get_tmux_pane_monitor", return_value=None):
        response = handler.handle_session_end(_make_event(S1))

    assert response.decision == "allow"
    assert instance_manager.get_for_session(S1) is None


def test_session_end_only_deletes_instances_for_target_session(db: HubDatabase) -> None:
    _ensure_session(db, S1)
    _ensure_session(db, S2)
    instance_manager = AgentStepInstanceManager(db)
    _save(
        instance_manager,
        session_id=S1,
        workflow_name="plan-adversary-steps",
    )
    _save(
        instance_manager,
        session_id=S2,
        workflow_name="developer",
    )

    handler = _SessionEndHandler(db)

    with patch("gobby.agents.tmux.get_tmux_pane_monitor", return_value=None):
        response = handler.handle_session_end(_make_event(S1))

    assert response.decision == "allow"
    assert instance_manager.get_for_session(S1) is None

    remaining = instance_manager.get_for_session(S2)
    assert remaining is not None
    assert remaining.agent_name == "developer"
