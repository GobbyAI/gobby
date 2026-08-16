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
from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager

pytestmark = pytest.mark.unit

# sessions.id, projects.id, and workflow_instances.id are native uuid columns.
SESSION_ID = "abababab-0000-4000-8000-000000000001"
PROJECT_ID = "abababab-0000-4000-8000-000000000002"
INSTANCE_ID = "abababab-0000-4000-8000-000000000004"


@pytest.fixture
def db(hub_db: HubDatabase) -> HubDatabase:
    return hub_db


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


@pytest.fixture
def engine(db: HubDatabase) -> RuleEngine:
    return RuleEngine(db)


@pytest.fixture
def instance_mgr(db: HubDatabase) -> AgentStepInstanceManager:
    return AgentStepInstanceManager(db)


def _make_event(
    *,
    event_type: HookEventType,
    session_id: str = SESSION_ID,
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


def _create_session_row(db: HubDatabase, session_id: str = SESSION_ID) -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        (PROJECT_ID, "test-project"),
    )
    db.execute(
        "INSERT INTO sessions "
        "(id, external_id, machine_id, source, project_id, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, "ext-1", "21000000-0000-4000-8000-000000000001", "claude", PROJECT_ID),
    )


def _insert_block_rule(
    db: HubDatabase,
    *,
    name: str,
    event: str,
    reason: str = "",
) -> RuleDefinitionRow:
    definition: dict[str, Any] = {
        "event": event,
        "effects": [
            {
                "type": "block",
                "reason": reason,
            }
        ],
    }
    return RuleDefinitionManager(db).create(
        name=name,
        definition_json=json.dumps(definition),
        priority=10,
    )


def _setup_step_workflow(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    instance_mgr: AgentStepInstanceManager,
    *,
    session_id: str = SESSION_ID,
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
        priority=100,
        enabled=True,
    )

    from gobby.workflows.agent_models import AgentDefinitionBody, AgentStepWorkflowBody
    from gobby.workflows.step_instances import build_step_instance

    instance_mgr.save(
        build_step_instance(
            AgentDefinitionBody(
                name="step-observability",
                surfaces=["spawn"],
                step_workflow=AgentStepWorkflowBody.model_validate(
                    {"steps": [{"name": "implement", "allowed_tools": ["Read"]}]}
                ),
            ),
            session_id=session_id,
            step_workflow_id=None,
            current_step="implement",
        )
    )


def _assert_block_records(
    caplog: pytest.LogCaptureFixture,
    *,
    expected_block: str,
    expected_fallback: str | None = None,
) -> None:
    block_records = [
        record for record in caplog.records if record.getMessage().startswith("BLOCK session=")
    ]
    assert len(block_records) == 1
    assert block_records[0].getMessage() == expected_block
    assert block_records[0].levelno == logging.DEBUG
    assert not any(record.levelno == logging.INFO for record in block_records)

    fallback_records = [
        record for record in caplog.records if record.getMessage().startswith("BLOCK fallback ")
    ]
    if expected_fallback is None:
        assert not fallback_records
        return

    assert len(fallback_records) == 1
    assert fallback_records[0].getMessage() == expected_fallback
    assert fallback_records[0].levelno == logging.WARNING


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
    rule = _insert_block_rule(db, name="test-empty-block-reason", event="stop", reason="")
    assert rule.priority == 10
    assert "priority" not in rule.definition_json
    event = _make_event(event_type=HookEventType.STOP)

    with caplog.at_level(logging.DEBUG):
        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

    assert response.decision == "block"
    assert response.reason
    assert "Blocked by rule" not in response.reason
    assert "Rule enforced by Gobby: [test-empty-block-reason]" in response.reason
    _assert_block_records(
        caplog,
        expected_block=(
            f"BLOCK session={SESSION_ID} event=stop tool=- source=rule "
            f"rule=test-empty-block-reason reason={response.reason}"
        ),
        expected_fallback=(
            f"BLOCK fallback session={SESSION_ID} event=stop tool=- source=rule "
            "rule=test-empty-block-reason detail=rule block effect omitted a reason"
        ),
    )


@pytest.mark.asyncio
async def test_step_enforcement_block_logs_structured_reason(
    db: HubDatabase,
    manager: RuleDefinitionManager,
    engine: RuleEngine,
    instance_mgr: AgentStepInstanceManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _setup_step_workflow(db, manager, instance_mgr)
    event = _make_event(
        event_type=HookEventType.BEFORE_TOOL,
        data={"tool_name": "Write"},
    )

    with caplog.at_level(logging.DEBUG):
        response = await engine.evaluate(event, session_id=SESSION_ID, variables={})

    assert response.decision == "block"
    assert response.reason
    _assert_block_records(
        caplog,
        expected_block=(
            f"BLOCK session={SESSION_ID} event=before_tool tool=Write "
            f"source=step-enforcement rule=step-tool-enforcement reason={response.reason}"
        ),
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
        with caplog.at_level(logging.DEBUG):
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
    _assert_block_records(
        caplog,
        expected_block=(
            f"BLOCK session={SESSION_ID} event=before_tool tool=Read source=webhook "
            f"rule=webhook-dispatch reason={response.reason}"
        ),
        expected_fallback=(
            f"BLOCK fallback session={SESSION_ID} event=before_tool tool=Read source=webhook "
            "rule=webhook-dispatch detail=blocking webhook omitted reason"
        ),
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

    with caplog.at_level(logging.DEBUG):
        result = await session._can_use_tool("Read", {}, ToolPermissionContext())

    assert isinstance(result, PermissionResultDeny)
    assert result.message
    assert result.message != "Blocked by session lifecycle"
    _assert_block_records(
        caplog,
        expected_block=(
            "BLOCK session=conv-observability event=before_tool tool=Read "
            f"source=session-lifecycle rule=pre-tool-callback reason={result.message}"
        ),
        expected_fallback=(
            "BLOCK fallback session=conv-observability event=before_tool tool=Read "
            "source=session-lifecycle rule=pre-tool-callback "
            "detail=pre-tool lifecycle callback omitted reason"
        ),
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

    with caplog.at_level(logging.DEBUG):
        response = await host._evaluate_blocking_webhooks(event)

    assert response is not None
    assert response["decision"] == "block"
    assert response["reason"]
    assert response["reason"] != "Blocked by webhook"
    _assert_block_records(
        caplog,
        expected_block=(
            "BLOCK session=web-chat-session event=before_tool tool=Read source=webhook "
            f"rule=webhook-dispatch reason={response['reason']}"
        ),
        expected_fallback=(
            "BLOCK fallback session=web-chat-session event=before_tool tool=Read source=webhook "
            "rule=webhook-dispatch detail=blocking webhook omitted reason"
        ),
    )
