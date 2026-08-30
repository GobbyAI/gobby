"""Tests for _fire_lifecycle CLI parity features (D1, D2, D6).

Covers:
- D1: Blocking webhook evaluation in _fire_lifecycle
- D2: Event broadcasting via HookEventBroadcaster
- D6: Inter-session message piggyback delivery
"""

from __future__ import annotations

from collections import Counter
from types import SimpleNamespace
from typing import Any, Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.adapters.agy import AgyAdapter
from gobby.adapters.agy_contract import AGY_HOOK_NAMES, get_agy_contract
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.events import (
    EVENT_TYPE_CLI_SUPPORT,
    HookEvent,
    HookEventType,
    HookResponse,
    SessionSource,
)
from gobby.servers.websocket.chat import ChatMixin
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.evaluation_runtime import WorkflowEvaluationRuntime
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules
from tests.fixtures.postgres import TEST_USER_ID

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class ChatMixinHost(ChatMixin):
    """Minimal host providing attributes ChatMixin expects."""

    def __init__(self) -> None:
        self.clients: dict[Any, Any] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, Any] = {}
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
        pass


@pytest.fixture
def host() -> ChatMixinHost:
    return ChatMixinHost()


@pytest.fixture
def rules_db(hub_db: HubDatabase) -> HubDatabase:
    return hub_db


# session_variables.session_id targets the native-uuid sessions.id column, so
# session ids that reach the real DB must be valid UUID strings.
SESSION_ID = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
PROJECT_ID = "dddddddd-dddd-4ddd-8ddd-ddddddddddd1"
MANAGED_FIRE_LIFECYCLE_PROVIDERS = ("claude", "codex", "droid", "grok", "qwen")


def _seed_session_row(db: HubDatabase, session_id: str = SESSION_ID) -> None:
    """Insert the project + session rows session_variables' FK requires."""
    project_id = PROJECT_ID
    machine_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
    db.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (%s, %s, NOW()) "
        "ON CONFLICT (id) DO NOTHING",
        (project_id, "parity-test-project"),
    )
    db.execute(
        "INSERT INTO machines (id, owner_user_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
        (machine_id, TEST_USER_ID),
    )
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (
            session_id,
            session_id,
            machine_id,
            "test",
            project_id,
        ),
    )


def _make_session(db_session_id: str = SESSION_ID, seq_num: int = 42) -> MagicMock:
    session = MagicMock()
    session.db_session_id = db_session_id
    session.seq_num = seq_num
    session.project_path = "/tmp/project"
    session.project_id = PROJECT_ID
    session.provider = "claude"
    return session


@pytest.mark.parametrize("provider", MANAGED_FIRE_LIFECYCLE_PROVIDERS)
@pytest.mark.asyncio
async def test_managed_provider_lifecycle_includes_runtime_chat_mode(
    host: ChatMixinHost, provider: str
) -> None:
    session = _make_session()
    session.provider = provider
    session.chat_mode = "plan"
    host._chat_sessions["conv-1"] = session
    host.workflow_handler = _make_workflow_handler()

    await host._fire_lifecycle(
        "conv-1",
        HookEventType.BEFORE_AGENT,
        {"prompt": "inspect the repository"},
    )

    event = host.workflow_handler.evaluate.call_args.args[0]
    assert event.metadata["session_type"] == "web_chat"
    assert event.metadata["chat_mode"] == "plan"


def _make_workflow_handler(
    decision: Literal["allow", "deny", "ask", "block", "modify"] = "allow",
    context: str | None = None,
) -> MagicMock:
    handler = MagicMock()
    response = HookResponse(decision=decision, context=context)
    handler.evaluate.return_value = response
    return handler


def _make_workflow_handler_with_response(**kwargs: Any) -> MagicMock:
    """Create a workflow handler returning a HookResponse with arbitrary kwargs."""
    handler = MagicMock()
    response = HookResponse(**kwargs)
    handler.evaluate.return_value = response
    return handler


# ---------------------------------------------------------------------------
# STOP/SUBAGENT_STOP enrichment exclusion (#18343)
# ---------------------------------------------------------------------------


class TestFireLifecycleStopEnrichment:
    """Session-ID enrichment must never touch STOP/SUBAGENT_STOP responses.

    Any non-empty Stop additionalContext re-engages the SDK agent, so the
    unconditional ``Gobby Session ID`` prefix turned every web-chat stop into
    a re-engagement loop (nine stop attempts on session #8891).
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.SUBAGENT_STOP])
    async def test_stop_without_context_gets_no_enrichment(
        self, host: ChatMixinHost, event_type: HookEventType
    ) -> None:
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle("conv-1", event_type, {"stop_hook_active": False})

        assert result is not None
        assert result["decision"] == "allow"
        assert result["context"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.SUBAGENT_STOP])
    async def test_stop_rule_context_preserved_without_session_prefix(
        self, host: ChatMixinHost, event_type: HookEventType
    ) -> None:
        # Deliberate rule-injected STOP context (e.g. notify-task-tree-complete)
        # must survive verbatim — that re-engagement is intentional.
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(
            context="Task tree complete: review the results."
        )

        result = await host._fire_lifecycle("conv-1", event_type, {"stop_hook_active": False})

        assert result is not None
        assert result["context"] == "Task tree complete: review the results."
        assert "Gobby Session ID" not in result["context"]

    @pytest.mark.asyncio
    async def test_before_agent_still_enriched(self, host: ChatMixinHost) -> None:
        host._chat_sessions["conv-1"] = _make_session(seq_num=42)
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_AGENT, {"prompt": "hello"}
        )

        assert result is not None
        assert result["context"] == "Gobby Session ID: #42"

    @pytest.mark.asyncio
    async def test_before_agent_enrichment_prefixes_rule_context(self, host: ChatMixinHost) -> None:
        host._chat_sessions["conv-1"] = _make_session(seq_num=42)
        host.workflow_handler = _make_workflow_handler(context="rule context")

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_AGENT, {"prompt": "hello"}
        )

        assert result is not None
        assert result["context"] == "Gobby Session ID: #42\n\nrule context"


# ---------------------------------------------------------------------------
# D1: Blocking Webhooks
# ---------------------------------------------------------------------------


class TestFireLifecycleBlockingWebhooks:
    """D1: _fire_lifecycle should evaluate blocking webhooks."""

    @pytest.mark.asyncio
    async def test_webhook_blocks_event(self, host: ChatMixinHost) -> None:
        """When a blocking webhook returns 'block', _fire_lifecycle returns block."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        # Setup webhook dispatcher that blocks
        endpoint = MagicMock()
        endpoint.enabled = True
        endpoint.can_block = True

        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = [endpoint]
        dispatcher._matches_event.return_value = True
        dispatcher._build_payload.return_value = {"event_type": "before_tool"}
        dispatcher._dispatch_single = AsyncMock(
            return_value=MagicMock(decision="block", response_body={"reason": "Policy violation"})
        )
        dispatcher.get_blocking_decision.return_value = ("block", "Policy violation")
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "block"
        assert result["reason"] == "Policy violation"

    @pytest.mark.asyncio
    async def test_webhook_allows_event(self, host: ChatMixinHost) -> None:
        """When blocking webhooks allow, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = []  # No matching endpoints
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_webhook_error_fails_open(self, host: ChatMixinHost) -> None:
        """Webhook evaluation errors should fail open (allow)."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = [MagicMock(enabled=True, can_block=True)]
        dispatcher._matches_event.side_effect = RuntimeError("Webhook crash")
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_no_webhook_dispatcher_proceeds(self, host: ChatMixinHost) -> None:
        """Without a webhook dispatcher, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_webhook_disabled_config_proceeds(self, host: ChatMixinHost) -> None:
        """When webhooks are disabled in config, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        dispatcher = MagicMock()
        dispatcher.config.enabled = False
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"


# ---------------------------------------------------------------------------
# D2: Event Broadcasting
# ---------------------------------------------------------------------------


class TestFireLifecycleBroadcasting:
    """D2: _fire_lifecycle should broadcast events for audit trail."""

    @pytest.mark.asyncio
    async def test_broadcasts_event_after_processing(self, host: ChatMixinHost) -> None:
        """_fire_lifecycle should call hook_broadcaster.broadcast_event."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()
        host.hook_broadcaster = broadcaster

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"
        broadcaster.broadcast_event.assert_awaited_once()

        # Verify event and response are passed
        call_args = broadcaster.broadcast_event.call_args
        event_arg = call_args[0][0]
        response_arg = call_args[0][1]
        assert isinstance(event_arg, HookEvent)
        assert event_arg.event_type == HookEventType.BEFORE_TOOL
        assert isinstance(response_arg, HookResponse)

    @pytest.mark.asyncio
    async def test_broadcast_error_does_not_crash(self, host: ChatMixinHost) -> None:
        """Broadcast errors should be swallowed (not crash lifecycle)."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock(side_effect=RuntimeError("Broadcast failed"))
        host.hook_broadcaster = broadcaster

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        # Should still return a valid result despite broadcast error
        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_no_broadcaster_proceeds(self, host: ChatMixinHost) -> None:
        """Without a broadcaster, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"


# ---------------------------------------------------------------------------
# D6: Inter-session Message Piggyback
# ---------------------------------------------------------------------------


class TestFireLifecycleMessagePiggyback:
    """D6: _fire_lifecycle should inject pending inter-session messages."""

    @pytest.mark.asyncio
    async def test_piggyback_on_before_tool(self, host: ChatMixinHost) -> None:
        """Pending messages should be injected into context on BEFORE_TOOL."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        msg = MagicMock()
        msg.content = "Agent A completed subtask #42"
        msg.id = "msg-1"
        msg.message_type = "message"
        msg.from_session = "aaaa1111-0000-0000-0000-000000000000"
        msg.priority = "normal"

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert "[Pending P2P messages from other sessions]:" in result["context"]
        assert "Agent A completed subtask #42" in result["context"]
        mgr.mark_delivered.assert_called_once_with("msg-1", SESSION_ID)

    @pytest.mark.asyncio
    async def test_piggyback_on_after_tool(self, host: ChatMixinHost) -> None:
        """Pending messages should be injected on AFTER_TOOL too."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        msg = MagicMock()
        msg.content = "Task completed"
        msg.id = "msg-2"
        msg.message_type = "message"
        msg.from_session = "bbbb2222-0000-0000-0000-000000000000"
        msg.priority = "normal"

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.AFTER_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert "Task completed" in result["context"]

    @pytest.mark.asyncio
    async def test_no_piggyback_on_session_start(self, host: ChatMixinHost) -> None:
        """Message piggyback should NOT run on SESSION_START events."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        mgr = MagicMock()
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.SESSION_START, {})

        assert result is not None
        mgr.get_undelivered_messages.assert_not_called()

    @pytest.mark.asyncio
    async def test_piggyback_on_before_agent(self, host: ChatMixinHost) -> None:
        """Message piggyback SHOULD run on BEFORE_AGENT — ensures messages
        arrive at agent turn start even before any tool calls."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        msg = MagicMock()
        msg.content = "Urgent update from coordinator"
        msg.id = "msg-agent-1"
        msg.message_type = "message"
        msg.from_session = "cccc3333-0000-0000-0000-000000000000"
        msg.priority = "normal"

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle("conv-1", HookEventType.BEFORE_AGENT, {"prompt": "hi"})

        assert result is not None
        assert "[Pending P2P messages from other sessions]:" in result["context"]
        assert "Urgent update from coordinator" in result["context"]
        mgr.mark_delivered.assert_called_once_with("msg-agent-1", SESSION_ID)

    @pytest.mark.asyncio
    async def test_no_pending_messages(self, host: ChatMixinHost) -> None:
        """When there are no pending messages, context is unchanged."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(context="rule context")

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = []
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert "Pending P2P messages" not in (result["context"] or "")
        assert "Pending messages from web chat" not in (result["context"] or "")

    @pytest.mark.asyncio
    async def test_piggyback_merges_with_existing_context(self, host: ChatMixinHost) -> None:
        """Pending messages should merge with existing rule/handler context."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(context="Rule context here")

        msg = MagicMock()
        msg.content = "Agent message"
        msg.id = "msg-3"
        msg.message_type = "message"
        msg.from_session = "dddd4444-0000-0000-0000-000000000000"
        msg.priority = "normal"

        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        # Should contain both rule context and message context
        assert "Rule context here" in result["context"]
        assert "Agent message" in result["context"]

    @pytest.mark.asyncio
    async def test_piggyback_error_fails_gracefully(self, host: ChatMixinHost) -> None:
        """Inter-session message errors should not crash lifecycle."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        mgr = MagicMock()
        mgr.get_undelivered_messages.side_effect = RuntimeError("DB error")
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_lifecycle_failure_leaves_piggyback_message_pending(
        self, host: ChatMixinHost
    ) -> None:
        """Messages remain pending when lifecycle processing fails after injection."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        msg = MagicMock(
            id="msg-retry",
            content="Retry this message",
            message_type="message",
            from_session="eeee5555-0000-0000-0000-000000000000",
            priority="normal",
        )
        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr
        host._dispatch_non_blocking_webhooks = AsyncMock(
            side_effect=RuntimeError("dispatch failed")
        )

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is None
        mgr.mark_delivered.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_manager_proceeds(self, host: ChatMixinHost) -> None:
        """Without inter_session_msg_manager, processing continues normally."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"


# ---------------------------------------------------------------------------
# Integration: All three features together
# ---------------------------------------------------------------------------


class TestFireLifecycleFullParity:
    """Test all parity features working together."""

    @pytest.mark.asyncio
    async def test_all_features_work_together(self, host: ChatMixinHost) -> None:
        """Webhook check + message piggyback + broadcasting all fire."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(context="workflow ctx")

        # Webhook dispatcher (allows)
        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = []
        host.webhook_dispatcher = dispatcher

        # Broadcaster
        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()
        host.hook_broadcaster = broadcaster

        # Inter-session messages
        msg = MagicMock()
        msg.content = "From agent"
        msg.id = "msg-99"
        msg.message_type = "message"
        msg.from_session = "eeee5555-0000-0000-0000-000000000000"
        msg.priority = "normal"
        mgr = MagicMock()
        mgr.get_undelivered_messages.return_value = [msg]
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"
        assert "workflow ctx" in result["context"]
        assert "From agent" in result["context"]
        broadcaster.broadcast_event.assert_awaited_once()
        mgr.mark_delivered.assert_called_once_with("msg-99", SESSION_ID)

    @pytest.mark.asyncio
    async def test_webhook_block_stops_everything(self, host: ChatMixinHost) -> None:
        """When webhook blocks, broadcasting and piggyback should NOT run."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        # Blocking webhook
        endpoint = MagicMock(enabled=True, can_block=True)
        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = [endpoint]
        dispatcher._matches_event.return_value = True
        dispatcher._build_payload.return_value = {}
        dispatcher._dispatch_single = AsyncMock(return_value=MagicMock(decision="block"))
        dispatcher.get_blocking_decision.return_value = ("block", "Denied")
        host.webhook_dispatcher = dispatcher

        # These should NOT be called
        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()
        host.hook_broadcaster = broadcaster

        mgr = MagicMock()
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "block"
        broadcaster.broadcast_event.assert_not_awaited()
        mgr.get_undelivered_messages.assert_not_called()


# ---------------------------------------------------------------------------
# Input Rewriting
# ---------------------------------------------------------------------------


class TestFireLifecycleRewriteInput:
    """rewrite_input effect should propagate modified_input and auto_approve to result."""

    @pytest.mark.asyncio
    async def test_rewrite_input_propagated(self, host: ChatMixinHost) -> None:
        """When response has modified_input, it appears in the result."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler_with_response(
            decision="allow",
            modified_input={"command": "gobby compress -- ls"},
            auto_approve=True,
        )

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["modified_input"] == {"command": "gobby compress -- ls"}
        assert result["auto_approve"] is True

    @pytest.mark.asyncio
    async def test_no_rewrite_input_when_absent(self, host: ChatMixinHost) -> None:
        """Standard response without modified_input should not have modified_input key."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert "modified_input" not in result


# ---------------------------------------------------------------------------
# Real Rule Path
# ---------------------------------------------------------------------------


class TestFireLifecycleRequireUvRule:
    """_fire_lifecycle should use real synced rules for shell command policy."""

    @pytest.mark.asyncio
    async def test_real_synced_require_uv_blocks_package_management_without_rewrite(
        self, host: ChatMixinHost, rules_db: HubDatabase
    ) -> None:
        """Web chat normalizes exec_command to Bash and returns a plain block."""
        sync_bundled_rules(rules_db, get_bundled_rules_path())
        _seed_session_row(rules_db)
        SessionVariableManager(rules_db).merge_variables(SESSION_ID, {"require_uv": True})

        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = WorkflowHookHandler(
            rule_engine=RuleEngine(rules_db),
            evaluation_runtime=WorkflowEvaluationRuntime(),
        )
        try:
            result = await host._fire_lifecycle(
                "conv-1",
                HookEventType.BEFORE_TOOL,
                {
                    "tool_name": "exec_command",
                    "tool_input": {"command": "python3.13 -m pip install requests"},
                },
            )
        finally:
            host.workflow_handler.shutdown()

        assert result is not None
        assert result["decision"] == "block"
        assert result["reason"] == (
            "Rule enforced by Gobby: [require-uv]\n"
            "Use `uv pip …` or `uv run python -m pip …` — "
            "uv manages this project's Python environment."
        )
        assert "modified_input" not in result
        assert "auto_approve" not in result


# ---------------------------------------------------------------------------
# Non-blocking Webhooks
# ---------------------------------------------------------------------------


class TestFireLifecycleNonBlockingWebhooks:
    """Non-blocking webhooks should be dispatched after processing."""

    @pytest.mark.asyncio
    async def test_non_blocking_webhooks_dispatched(self, host: ChatMixinHost) -> None:
        """Non-blocking endpoints should be dispatched after processing."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        endpoint = MagicMock(enabled=True, can_block=False)
        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = [endpoint]
        dispatcher._matches_event.return_value = True
        dispatcher._build_payload.return_value = {"event_type": "before_tool"}
        dispatcher._dispatch_single = AsyncMock(return_value=MagicMock(success=True))
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"
        dispatcher._dispatch_single.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_blocking_webhook_error_ignored(self, host: ChatMixinHost) -> None:
        """Non-blocking webhook errors should not crash lifecycle."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler()

        endpoint = MagicMock(enabled=True, can_block=False)
        dispatcher = MagicMock()
        dispatcher.config.enabled = True
        dispatcher.config.endpoints = [endpoint]
        dispatcher._matches_event.side_effect = RuntimeError("Webhook boom")
        host.webhook_dispatcher = dispatcher

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "allow"

    @pytest.mark.asyncio
    async def test_workflow_block_stops_everything(self, host: ChatMixinHost) -> None:
        """When workflow blocks, webhooks, broadcasting, and piggyback should NOT run."""
        host._chat_sessions["conv-1"] = _make_session()
        host.workflow_handler = _make_workflow_handler(decision="block")

        # These should NOT be called
        dispatcher = MagicMock()
        host.webhook_dispatcher = dispatcher

        broadcaster = MagicMock()
        broadcaster.broadcast_event = AsyncMock()
        host.hook_broadcaster = broadcaster

        mgr = MagicMock()
        host.inter_session_msg_manager = mgr

        result = await host._fire_lifecycle(
            "conv-1", HookEventType.BEFORE_TOOL, {"tool_name": "bash"}
        )

        assert result is not None
        assert result["decision"] == "block"
        # Webhook dispatcher should never be touched
        dispatcher._matches_event.assert_not_called()
        broadcaster.broadcast_event.assert_not_awaited()
        mgr.get_undelivered_messages.assert_not_called()


class _RecordingAgyHookManager:
    """Count each HookManager.handle side-effect once per native event."""

    def __init__(self) -> None:
        self.events: list[HookEvent] = []
        self.rule_effects: list[str] = []
        self.mcp_dispatches: list[str] = []
        self.handler_contexts: list[str] = []
        self.pending_deliveries: list[str] = []
        self.webhooks: list[str] = []
        self.broadcasts: list[str] = []

    def handle(self, event: HookEvent) -> HookResponse:
        self.events.append(event)
        key = event.event_type.value
        self.rule_effects.append(key)
        self.mcp_dispatches.append(key)
        self.handler_contexts.append(key)
        self.pending_deliveries.append(key)
        self.webhooks.append(key)
        self.broadcasts.append(key)
        if event.event_type is HookEventType.BEFORE_TOOL:
            return HookResponse(
                decision="block",
                reason="blocked-once",
                context="injected-once",
                modified_input={"command": "echo once"},
            )
        if event.event_type is HookEventType.SESSION_START:
            return HookResponse(decision="allow", context="startup-context", system_message="hi")
        if event.event_type is HookEventType.BEFORE_AGENT:
            return HookResponse(decision="allow", context="before-agent-context")
        return HookResponse(decision="allow", context=f"ctx-{key}")


def _agy_native_event(hook_type: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    input_data: dict[str, Any] = {
        "hookEventName": hook_type,
        "conversationId": "agy-conv-1",
        "transcriptPath": "/tmp/agy.jsonl",
        "workspacePaths": ["/repo"],
    }
    if extra:
        input_data.update(extra)
    return {"source": "agy", "hook_type": hook_type, "input_data": input_data}


def _launch_owner() -> SimpleNamespace:
    return SimpleNamespace(
        _chat_sessions={},
        clients={},
        web_chat_session_registry=None,
        _fire_lifecycle=AsyncMock(return_value=None),
    )


def _launch_session() -> MagicMock:
    session = MagicMock()
    session.start = AsyncMock()
    session.resume_session_id = None
    session.db_session_id = None
    session.model = None
    session.chat_mode = "plan"
    session.sandbox_metadata = {}
    return session


class TestAgyNativeLifecycleParity:
    def test_agy_is_outside_managed_fire_lifecycle_registry(self) -> None:
        assert "agy" not in MANAGED_FIRE_LIFECYCLE_PROVIDERS
        assert MANAGED_FIRE_LIFECYCLE_PROVIDERS == (
            "claude",
            "codex",
            "droid",
            "grok",
            "qwen",
        )

    def test_pre_compact_unsupported_for_agy(self) -> None:
        assert EVENT_TYPE_CLI_SUPPORT[HookEventType.PRE_COMPACT]["agy"] is None
        assert "PreCompact" not in AGY_HOOK_NAMES
        assert get_agy_contract("PreCompact") is None
        assert get_agy_contract("PRE_COMPACT") is None

    @pytest.mark.asyncio
    async def test_compaction_context_uses_parsed_provider(self, host: ChatMixinHost) -> None:
        session = _make_session()
        session.provider = "codex"
        host._chat_sessions["conv-1"] = session
        host.workflow_handler = _make_workflow_handler()

        result = await host._fire_lifecycle("conv-1", HookEventType.PRE_COMPACT, {})

        assert result is not None
        assert "Source: codex" in (result.get("context") or "")
        assert "Source: claude" not in (result.get("context") or "")

    @pytest.mark.asyncio
    async def test_session_start_prefire_suppressed_for_agy(self) -> None:
        from gobby.agents.sandbox import SandboxConfig
        from gobby.servers.websocket.chat._session_launch import (
            SessionLaunchContext,
            start_hydrated_session,
        )
        from gobby.servers.websocket.chat.runtime_manager import SandboxPolicySnapshot
        from tests._timing import drain_asyncio_tasks

        owner = _launch_owner()
        session = _launch_session()
        context = SessionLaunchContext(
            sandbox=SandboxPolicySnapshot(
                config=SandboxConfig(enabled=False),
                policy_hash="hash",
            ),
            workspace_path="/tmp/ws",
        )

        await start_hydrated_session(
            owner,
            session,
            context,
            session_key="conv-agy",
            effective_model=None,
            persona_selected=False,
            pending_agent=None,
            pending_mode="plan",
            agent_name="agy",
            provider_name="agy",
            session_manager=None,
            existing_db_session=None,
            project_context_changed=False,
            effective_pid="proj",
        )
        await drain_asyncio_tasks()

        assert owner._chat_sessions["conv-agy"] is session
        session.start.assert_awaited_once()
        assert session.project_path == "/tmp/ws"
        owner._fire_lifecycle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_session_start_prefire_still_fires_for_incumbent(self) -> None:
        from gobby.agents.sandbox import SandboxConfig
        from gobby.servers.websocket.chat._session_launch import (
            SessionLaunchContext,
            start_hydrated_session,
        )
        from gobby.servers.websocket.chat.runtime_manager import SandboxPolicySnapshot
        from tests._timing import drain_asyncio_tasks

        owner = _launch_owner()
        session = _launch_session()
        context = SessionLaunchContext(
            sandbox=SandboxPolicySnapshot(
                config=SandboxConfig(enabled=False),
                policy_hash="hash",
            ),
            workspace_path="/tmp/ws",
        )

        await start_hydrated_session(
            owner,
            session,
            context,
            session_key="conv-claude",
            effective_model=None,
            persona_selected=False,
            pending_agent=None,
            pending_mode="plan",
            agent_name="claude",
            provider_name="claude",
            session_manager=None,
            existing_db_session=None,
            project_context_changed=False,
            effective_pid="proj",
        )
        await drain_asyncio_tasks()

        assert owner._chat_sessions["conv-claude"] is session
        session.start.assert_awaited_once()
        owner._fire_lifecycle.assert_awaited_once_with(
            "conv-claude",
            HookEventType.SESSION_START,
            {},
        )

    def test_first_preinvocation_is_single_native_authority(self) -> None:
        manager = _RecordingAgyHookManager()

        result = AgyAdapter().handle_native(
            _agy_native_event("PreInvocation", {"invocationNum": 0}),
            cast(HookManager, manager),
        )

        types = [event.event_type for event in manager.events]
        assert types == [HookEventType.SESSION_START, HookEventType.BEFORE_AGENT]
        assert Counter(types) == Counter(
            {HookEventType.SESSION_START: 1, HookEventType.BEFORE_AGENT: 1}
        )
        assert all(event.source is SessionSource.AGY for event in manager.events)
        assert "startup-context" in str(result)
        assert "before-agent-context" in str(result)

    def test_native_route_side_effects_fire_once_per_event(self) -> None:
        manager = _RecordingAgyHookManager()
        adapter = AgyAdapter()

        hooked = cast(HookManager, manager)
        pre_tool = adapter.handle_native(
            _agy_native_event(
                "PreToolUse",
                {"toolCall": {"name": "run_shell_command", "args": {"command": "pwd"}}},
            ),
            hooked,
        )
        adapter.handle_native(_agy_native_event("PostToolUse"), hooked)
        adapter.handle_native(_agy_native_event("Stop"), hooked)

        types = [event.event_type for event in manager.events]
        assert HookEventType.PRE_COMPACT not in types
        assert Counter(types) == Counter(
            {
                HookEventType.BEFORE_TOOL: 1,
                HookEventType.AFTER_TOOL: 1,
                HookEventType.STOP: 1,
            }
        )
        assert all(event.source is SessionSource.AGY for event in manager.events)
        for bucket in (
            manager.rule_effects,
            manager.mcp_dispatches,
            manager.handler_contexts,
            manager.pending_deliveries,
            manager.webhooks,
            manager.broadcasts,
        ):
            assert Counter(bucket) == Counter(
                {
                    HookEventType.BEFORE_TOOL.value: 1,
                    HookEventType.AFTER_TOOL.value: 1,
                    HookEventType.STOP.value: 1,
                }
            )
        assert pre_tool.get("decision") == "deny"
        before_tool = manager.events[0]
        assert before_tool.event_type is HookEventType.BEFORE_TOOL
