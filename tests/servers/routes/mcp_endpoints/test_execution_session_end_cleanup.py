"""Regression coverage for parent-scoped MCP calls targeting child sessions."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers._session_end import SessionEndMixin
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.servers.routes.mcp.endpoints.execution import _set_context_for_request
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.utils.session_context import reset_seeded_contexts
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit


_PLAN_ADVERSARY_TERMINATE_WORKFLOW = {
    "name": "plan-adversary-steps",
    "description": "Step-enforcement workflow used by plan-adversary agents.",
    "steps": [
        {
            "name": "terminate",
            "description": "Only kill_agent is allowed while the child tears down.",
            "allowed_tools": ["mcp__gobby__call_tool"],
            "allowed_mcp_tools": ["gobby-agents:kill_agent"],
        }
    ],
    "exit_condition": "current_step == 'terminate'",
}


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    database = hub_db
    database.execute(
        "INSERT INTO projects (id, name) VALUES (?, ?)",
        ("proj1", "test-project"),
    )
    return database


def _insert_session(
    db: HubDatabase,
    *,
    session_id: str,
    external_id: str,
    project_id: str = "proj1",
) -> None:
    db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (session_id, external_id, "machine-1", "codex", project_id),
    )


def _make_request(*, header_session_id: str) -> MagicMock:
    request = MagicMock()
    request.headers = {"x-gobby-session-id": header_session_id}
    return request


class _SessionEndHandler(SessionEndMixin):
    def __init__(
        self,
        *,
        session_manager: SessionManager,
        workflow_handler: WorkflowHookHandler,
    ) -> None:
        self.logger = MagicMock()
        self._session_manager = cast(Any, session_manager)
        self._workflow_handler = workflow_handler
        self._session_storage = session_manager
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


def _make_session_end_event(session_id: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.SESSION_END,
        session_id=f"ext-{session_id}",
        source=SessionSource.CODEX,
        timestamp=datetime.now(),
        data={},
        metadata={"_platform_session_id": session_id},
    )


@pytest.mark.parametrize(
    ("tool_name", "expected_fragment"),
    [
        ("get_session_messages", "gobby-sessions:get_session_messages"),
        ("get_transcript_status", "gobby-sessions:get_transcript_status"),
    ],
)
@pytest.mark.asyncio
async def test_session_end_cleanup_unblocks_session_targeted_read_only_calls(
    db: HubDatabase,
    tool_name: str,
    expected_fragment: str,
) -> None:
    parent_session_id = str(uuid.uuid4())
    child_session_id = str(uuid.uuid4())

    _insert_session(
        db,
        session_id=parent_session_id,
        external_id=f"ext-{parent_session_id}",
    )
    _insert_session(
        db,
        session_id=child_session_id,
        external_id=f"ext-{child_session_id}",
    )

    LocalWorkflowDefinitionManager(db).create(
        name="plan-adversary-steps",
        definition_json=json.dumps(_PLAN_ADVERSARY_TERMINATE_WORKFLOW),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )
    instance_manager = WorkflowInstanceManager(db)
    instance_manager.save_instance(
        WorkflowInstance(
            id="inst-child-terminate",
            session_id=child_session_id,
            workflow_name="plan-adversary-steps",
            enabled=True,
            priority=100,
            current_step="terminate",
            step_entered_at=datetime.now(UTC),
        )
    )

    session_manager = SessionManager(db)
    workflow_handler = WorkflowHookHandler(rule_engine=RuleEngine(db=db), enabled=True)
    hook_manager = SimpleNamespace(
        _workflow_handler=workflow_handler,
        _session_manager=session_manager,
        _session_storage=session_manager,
        _database=db,
    )
    tool_proxy = ToolProxyService(
        mcp_manager=MagicMock(),
        validate_arguments=False,
        hook_manager_resolver=lambda: cast(Any, hook_manager),
    )

    server = MagicMock()
    server.session_manager = session_manager
    request = _make_request(header_session_id=parent_session_id)
    tokens = _set_context_for_request(server, {"session_id": child_session_id}, request)
    assert tokens.resolved_session_id == parent_session_id

    try:
        _, _, _, parent_allowed = await tool_proxy._apply_before_tool_enforcement(
            server_name="gobby-sessions",
            tool_name=tool_name,
            arguments={"session_id": child_session_id},
            session_id=None,
        )

        assert parent_allowed is None

        _, _, _, blocked = await tool_proxy._apply_before_tool_enforcement(
            server_name="gobby-sessions",
            tool_name=tool_name,
            arguments={"session_id": child_session_id},
            session_id=child_session_id,
        )

        assert blocked is not None
        assert expected_fragment in blocked["error"]

        handler = _SessionEndHandler(
            session_manager=session_manager,
            workflow_handler=workflow_handler,
        )
        with patch("gobby.agents.tmux.get_tmux_pane_monitor", return_value=None):
            response = handler.handle_session_end(_make_session_end_event(child_session_id))
        assert response.decision == "allow"
        assert instance_manager.get_active_instances(child_session_id) == []

        _, _, _, allowed = await tool_proxy._apply_before_tool_enforcement(
            server_name="gobby-sessions",
            tool_name=tool_name,
            arguments={"session_id": child_session_id},
            session_id=child_session_id,
        )

        assert allowed is None
    finally:
        reset_seeded_contexts(tokens)
