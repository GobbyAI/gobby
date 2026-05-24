"""Regression coverage for structured Gobby-side block observability."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

from gobby.hooks.dispatchers.webhook import evaluate_blocking_webhooks
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.servers.chat_session import ChatSession
from gobby.servers.websocket.chat import ChatMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    return hub_db


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def engine(db: HubDatabase) -> RuleEngine:
    return RuleEngine(db)


@pytest.fixture
def instance_mgr(db: HubDatabase) -> WorkflowInstanceManager:
    return WorkflowInstanceManager(db)


def _make_event(
    *,
    event_type: HookEventType,
    session_id: str = "test-session",
    data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )


def _create_session_row(db: HubDatabase, session_id: str = "test-session") -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        ("project-1", "test-project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, "ext-1", "machine-1", "claude", "project-1"),
    )


def _insert_block_rule(
    db: HubDatabase,
    *,
    name: str,
    event: str,
    reason: str = "",
) -> None:
    definition: dict[str, Any] = {
        "event": event,
        "priority": 10,
        "effects": [
            {
                "type": "block",
                "reason": reason,
            }
        ],
    }
    db.execute(
        """
        INSERT INTO workflow_definitions (name, workflow_type, definition_json, enabled, source)
        VALUES (?, 'rule', ?, 1, 'test')
        """,
        (name, json.dumps(definition)),
    )


def _setup_step_workflow(
    db: HubDatabase,
    manager: LocalWorkflowDefinitionManager,
    instance_mgr: WorkflowInstanceManager,
    *,
    session_id: str = "test-session",
) -> None:
    _create_session_row(db, session_id)
    definition: dict[str, Any] = {
        "name": "step-observability",
        "version": "2.0",
        "enabled": False,
        "steps": [
            {
                "name": "implement",
                "allowed_tools": ["Read"],
            }
        ],
    }
    manager.create(
        name=definition["name"],
        definition_json=json.dumps(definition),
        workflow_type="workflow",
        priority=100,
        enabled=True,
    )

    from gobby.workflows.definitions import WorkflowInstance

    instance = WorkflowInstance(
        id=f"inst-{session_id}-step-observability",
        session_id=session_id,
        workflow_name="step-observability",
        enabled=True,
        priority=100,
        current_step="implement",
        step_entered_at=datetime.now(UTC),
        variables={},
    )
    instance_mgr.save_instance(instance)


def _block_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if "BLOCK " in record.getMessage()]


class ChatLifecycleHost(ChatMixin):
    """Minimal host exposing lifecycle mixin methods for unit tests."""

    def __init__(self) -> None:
        self.clients: dict[Any, Any] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, Any] = {}
        self._pending_modes: dict[str, str] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._pending_projects: dict[str, str] = {}
        self.workflow_handler: Any = None
        self.event_handlers: Any = None
        self.webhook_dispatcher: Any = None
        self.hook_broadcaster: Any = None
        self.inter_session_msg_manager: Any = None
        self.mcp_manager: Any = None
        self.internal_manager: Any = None

    async def _send_error(
        self,
        websocket: object,
        message: str,
        request_id: str | None = None,
        code: str = "ERROR",
    ) -> None:
        return None


@pytest.mark.asyncio
async def test_rule_block_reason_and_log_are_structured(
    db: HubDatabase,
    engine: RuleEngine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _insert_block_rule(db, name="test-empty-block-reason", event="stop", reason="")
    event = _make_event(event_type=HookEventType.STOP)

    with caplog.at_level(logging.INFO):
        response = await engine.evaluate(event, session_id="test-session", variables={})

    assert response.decision == "block"
    assert response.reason
    assert "Blocked by rule" not in response.reason
    assert "Rule enforced by Gobby: [test-empty-block-reason]" in response.reason
    assert any(
        "BLOCK session=test-session event=stop tool=- source=rule "
        "rule=test-empty-block-reason reason=Rule enforced by Gobby: "
        "[test-empty-block-reason]" in message
        for message in _block_messages(caplog)
    )
    assert any(
        "detail=rule block effect omitted a reason" in message
        for message in _block_messages(caplog)
    )


@pytest.mark.asyncio
async def test_step_enforcement_block_logs_structured_reason(
    db: HubDatabase,
    manager: LocalWorkflowDefinitionManager,
    engine: RuleEngine,
    instance_mgr: WorkflowInstanceManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _setup_step_workflow(db, manager, instance_mgr)
    event = _make_event(
        event_type=HookEventType.BEFORE_TOOL,
        data={"tool_name": "Write"},
    )

    with caplog.at_level(logging.INFO):
        response = await engine.evaluate(event, session_id="test-session", variables={})

    assert response.decision == "block"
    assert response.reason
    assert any(
        "BLOCK session=test-session event=before_tool tool=Write "
        "source=step-enforcement rule=step-tool-enforcement" in message
        for message in _block_messages(caplog)
    )


def test_webhook_block_reason_and_log_are_structured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _make_event(
        event_type=HookEventType.BEFORE_TOOL,
        data={"tool_name": "Read"},
    )
    dispatcher = MagicMock()
    dispatcher.get_blocking_decision.return_value = ("block", None)

    with patch(
        "gobby.hooks.dispatchers.webhook.dispatch_webhooks_sync",
        return_value=[MagicMock()],
    ):
        with caplog.at_level(logging.INFO):
            response = evaluate_blocking_webhooks(
                event,
                dispatcher,
                logging.getLogger("tests.block_observability.webhook"),
                None,
            )

    assert response is not None
    assert response.decision == "block"
    assert response.reason
    assert response.reason != "Blocked by webhook"
    assert any(
        "BLOCK session=test-session event=before_tool tool=Read "
        "source=webhook rule=webhook-dispatch" in message
        for message in _block_messages(caplog)
    )
    assert any(
        "detail=blocking webhook omitted reason" in message for message in _block_messages(caplog)
    )


@pytest.mark.asyncio
async def test_session_lifecycle_block_reason_and_log_are_structured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = ChatSession(conversation_id="conv-observability")
    session.chat_mode = "normal"
    session._tool_approval_config = None
    session._plan_approved = False
    session._on_pre_tool = AsyncMock(return_value={"decision": "block"})

    with caplog.at_level(logging.INFO):
        result = await session._can_use_tool("Read", {}, ToolPermissionContext())

    assert isinstance(result, PermissionResultDeny)
    assert result.message
    assert result.message != "Blocked by session lifecycle"
    assert any(
        "BLOCK session=conv-observability event=before_tool tool=Read "
        "source=session-lifecycle rule=pre-tool-callback" in message
        for message in _block_messages(caplog)
    )
    assert any(
        "detail=pre-tool lifecycle callback omitted reason" in message
        for message in _block_messages(caplog)
    )


@pytest.mark.asyncio
async def test_websocket_chat_webhook_block_reason_and_log_are_structured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = ChatLifecycleHost()
    endpoint = MagicMock(enabled=True, can_block=True)
    dispatcher = MagicMock()
    dispatcher.config.enabled = True
    dispatcher.config.endpoints = [endpoint]
    dispatcher._matches_event.return_value = True
    dispatcher._build_payload.return_value = {"event_type": "before_tool"}
    dispatcher._dispatch_single = AsyncMock(return_value=MagicMock())
    dispatcher.get_blocking_decision.return_value = ("block", None)
    host.webhook_dispatcher = dispatcher
    event = _make_event(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="web-chat-session",
        data={"tool_name": "Read"},
    )

    with caplog.at_level(logging.INFO):
        response = await host._evaluate_blocking_webhooks(event)

    assert response is not None
    assert response["decision"] == "block"
    assert response["reason"]
    assert response["reason"] != "Blocked by webhook"
    assert any(
        "BLOCK session=web-chat-session event=before_tool tool=Read "
        "source=webhook rule=webhook-dispatch" in message
        for message in _block_messages(caplog)
    )
    assert any(
        "detail=blocking webhook omitted reason" in message for message in _block_messages(caplog)
    )
