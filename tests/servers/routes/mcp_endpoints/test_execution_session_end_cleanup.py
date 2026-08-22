"""Regression coverage for parent-scoped MCP calls targeting child sessions."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from gobby.config.app import DaemonConfig
from gobby.hooks.event_handlers._session_end import SessionEndMixin
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.inbox import drain_hook_inbox_once
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.servers.routes.mcp.endpoints.request_context import _set_context_for_request
from gobby.servers.routes.mcp.hooks import create_hooks_router
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import reset_seeded_contexts
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit

# projects.id is a native uuid column; use a valid UUID string.
PROJECT_ID = "11111111-1111-4111-8111-111111111111"


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
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "test-project"),
    )
    return database


def _insert_session(
    db: HubDatabase,
    *,
    session_id: str,
    external_id: str,
    project_id: str = PROJECT_ID,
) -> None:
    db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (session_id, external_id, "21000000-0000-4000-8000-000000000001", "codex", project_id),
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
        self._session_manager = session_manager
        self._workflow_handler = workflow_handler
        self._session_storage = session_manager
        self._session_coordinator = None
        self._session_end_auto_link_worker = None
        self._message_processor_resolver = lambda: None
        self._session_message_processors: dict[str, object] = {}
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
        self.handle_count = 0

    def handle(self, event: HookEvent) -> HookResponse:
        self.handle_count += 1
        return self.handle_session_end(event)


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

    RuleDefinitionManager(db).create(
        name="plan-adversary-steps",
        definition_json=json.dumps(_PLAN_ADVERSARY_TERMINATE_WORKFLOW),
        priority=100,
        enabled=True,
    )
    instance_manager = AgentStepInstanceManager(db)
    from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody
    from gobby.workflows.step_instances import build_step_instance

    instance_manager.save(
        build_step_instance(
            AgentDefinitionBody(
                prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                name="plan-adversary",
                surfaces=["spawn"],
                step_workflow=AgentStepWorkflowBody.model_validate(
                    {
                        "exit_condition": _PLAN_ADVERSARY_TERMINATE_WORKFLOW["exit_condition"],
                        "steps": _PLAN_ADVERSARY_TERMINATE_WORKFLOW["steps"],
                    }
                ),
            ),
            session_id=child_session_id,
            step_workflow_id=None,
            current_step="terminate",
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
        hook_manager_resolver=lambda: cast("HookManager", hook_manager),
    )

    server = MagicMock()
    server.session_manager = session_manager
    server.run_db = AsyncMock(side_effect=lambda func, *args, **kwargs: func(*args, **kwargs))
    request = _make_request(header_session_id=parent_session_id)
    tokens = await _set_context_for_request(server, {"session_id": child_session_id}, request)
    assert tokens.resolved_session_id == parent_session_id

    try:
        _, _, _, parent_allowed, _ = await tool_proxy._apply_before_tool_enforcement(
            server_name="gobby-sessions",
            tool_name=tool_name,
            arguments={"session_id": child_session_id},
            session_id=None,
        )

        assert parent_allowed is None

        _, _, _, blocked, _ = await tool_proxy._apply_before_tool_enforcement(
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
        assert instance_manager.get_for_session(child_session_id) is None

        _, _, _, allowed, _ = await tool_proxy._apply_before_tool_enforcement(
            server_name="gobby-sessions",
            tool_name=tool_name,
            arguments={"session_id": child_session_id},
            session_id=child_session_id,
        )

        assert allowed is None
    finally:
        reset_seeded_contexts(tokens)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_inbox_replays_codex_session_end_once_with_real_cleanup(
    db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = str(uuid.uuid4())
    external_id = f"ext-{session_id}"
    _insert_session(db, session_id=session_id, external_id=external_id)

    RuleDefinitionManager(db).create(
        name="plan-adversary-steps",
        definition_json=json.dumps(_PLAN_ADVERSARY_TERMINATE_WORKFLOW),
        priority=100,
        enabled=True,
    )
    workflow_handler = WorkflowHookHandler(rule_engine=RuleEngine(db=db), enabled=True)
    instance_manager = AgentStepInstanceManager(db)
    instance_manager.save(
        make_step_instance(
            session_id,
            agent_name="plan-adversary",
            current_step="terminate",
        )
    )
    session_manager = SessionManager(db)
    hook_manager = _SessionEndHandler(
        session_manager=session_manager,
        workflow_handler=workflow_handler,
    )

    server = MagicMock()
    server.config = DaemonConfig()
    app = FastAPI()
    app.state.hook_manager = hook_manager
    app.include_router(create_hooks_router(server))

    gobby_home = tmp_path / "gobby-home"
    inbox_dir = gobby_home / "hooks" / "inbox"
    inbox_dir.mkdir(parents=True)
    monkeypatch.setenv("GOBBY_HOME", str(gobby_home))
    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-07-26T20:00:00Z",
        "critical": False,
        "hook_type": "SessionEnd",
        "input_data": {
            "session_id": external_id,
            "reason": "other",
        },
        "source": "codex",
        "headers": {
            "X-Gobby-Project-Id": PROJECT_ID,
            "X-Gobby-Session-Id": session_id,
        },
    }
    envelope_path = inbox_dir / "n-0000000000001-session-end.json"
    envelope_path.write_text(json.dumps(envelope))
    os.utime(envelope_path, (0, 0))

    with (
        patch("gobby.hooks.inbox.read_local_api_token", return_value="test-local-token"),
        patch("gobby.agents.tmux.get_tmux_pane_monitor", return_value=None),
    ):
        first_replay = await drain_hook_inbox_once(app, inbox_dir=inbox_dir)
        second_replay = await drain_hook_inbox_once(app, inbox_dir=inbox_dir)

    assert first_replay == 1
    assert second_replay == 0
    assert hook_manager.handle_count == 1
    assert not envelope_path.exists()
    stored_session = session_manager.get(session_id)
    assert stored_session is not None
    assert stored_session.status == "expired"
    assert instance_manager.get_for_session(session_id) is None
