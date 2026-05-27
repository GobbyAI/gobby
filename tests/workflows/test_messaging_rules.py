"""Tests for messaging rules.

Covers:
- deliver-pending-messages: calls MCP on turn_start
- notify-unread-mail: injects context nudge when agent has pending messages
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.enforcement.blocking import is_message_delivery_tool
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
    platform_session_id: str | None = "plat-sess-1",
) -> HookEvent:
    metadata = {}
    if platform_session_id is not None:
        metadata["_platform_session_id"] = platform_session_id

    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata,
    )


def _insert_rule(
    manager: LocalWorkflowDefinitionManager,
    name: str,
    body: RuleDefinitionBody,
    priority: int = 100,
) -> str:
    row = manager.create(
        name=name,
        definition_json=body.model_dump_json(),
        workflow_type="rule",
        priority=priority,
        enabled=True,
    )
    return row.id


def _sync_bundled(db: HubDatabase) -> None:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

    sync_bundled_rules(db, get_bundled_rules_path())
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")


# ═══════════════════════════════════════════════════════════════════════
# deliver-pending-messages
# ═══════════════════════════════════════════════════════════════════════


class TestDeliverPendingMessages:
    """deliver-pending-messages calls MCP on turn_start for sessions with platform ids."""

    def _rule_body(self) -> RuleDefinitionBody:
        return RuleDefinitionBody(
            event=RuleEvent.TURN_START,
            when="event.metadata.get('_platform_session_id')",
            effects=[
                RuleEffect(
                    type="mcp_call",
                    server="gobby-agents",
                    tool="deliver_pending_messages",
                    arguments={
                        "target_session_id": "{{ event.metadata.get('_platform_session_id', '') }}"
                    },
                    inject_result=True,
                )
            ],
        )

    def test_bundled_rule_matches_parent_delivery_contract(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("deliver-pending-messages")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert row.enabled is True
        assert row.priority == 10
        assert body.event.value == "turn_start"
        assert body.when is not None
        assert "event.metadata.get('_platform_session_id')" in body.when
        assert "is_spawned_agent" not in body.when

        effect = body.effects[0]
        assert effect.type == "mcp_call"
        assert effect.server == "gobby-agents"
        assert effect.tool == "deliver_pending_messages"
        assert effect.arguments == {
            "target_session_id": "{{ event.metadata.get('_platform_session_id', '') }}"
        }
        assert effect.arguments["target_session_id"] != "{{ event.session_id }}"
        assert effect.inject_result is True

    @pytest.mark.asyncio
    async def test_fires_on_before_agent(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "deliver-pending-messages", self._rule_body(), priority=10)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        variables: dict[str, Any] = {"servers_listed": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 1

    @pytest.mark.asyncio
    async def test_records_correct_mcp_call(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "deliver-pending-messages", self._rule_body(), priority=10)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        variables: dict[str, Any] = {"servers_listed": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        call = response.metadata["mcp_calls"][0]
        assert call["server"] == "gobby-agents"
        assert call["tool"] == "deliver_pending_messages"
        assert call["arguments"] == {"target_session_id": "plat-sess-1"}
        assert call["inject_result"] is True

    @pytest.mark.asyncio
    async def test_fires_for_parent_session_without_spawned_agent_flag(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "deliver-pending-messages", self._rule_body(), priority=10)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        response = await engine.evaluate(
            event, session_id="sess-1", variables={"servers_listed": True}
        )

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 1

    @pytest.mark.asyncio
    async def test_skips_when_platform_session_id_is_missing(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "deliver-pending-messages", self._rule_body(), priority=10)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT, platform_session_id=None)
        variables: dict[str, Any] = {"is_spawned_agent": True, "servers_listed": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# is_message_delivery_tool
# ═══════════════════════════════════════════════════════════════════════


class TestIsMessageDeliveryTool:
    """Unit tests for is_message_delivery_tool helper."""

    def test_recognises_deliver_pending_messages(self) -> None:
        assert is_message_delivery_tool("deliver_pending_messages") is True

    def test_rejects_other_tools(self) -> None:
        assert is_message_delivery_tool("list_tools") is False
        assert is_message_delivery_tool("Edit") is False

    def test_none_returns_false(self) -> None:
        assert is_message_delivery_tool(None) is False


# ═══════════════════════════════════════════════════════════════════════
# notify-unread-mail helpers
# ═══════════════════════════════════════════════════════════════════════

_SENDER_SESSION = "sender-session-aaa"
_TEST_PROJECT_ID = "test-project-001"


def _ensure_project(db: HubDatabase) -> None:
    """Insert a minimal project row so session FK constraints are satisfied."""
    db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, 'test-project') ON CONFLICT (id) DO NOTHING",
        (_TEST_PROJECT_ID,),
    )


def _create_session(db: HubDatabase, session_id: str) -> None:
    """Insert a minimal session row so FK constraints are satisfied."""
    _ensure_project(db)
    db.execute(
        "INSERT INTO sessions (id, external_id, machine_id, source, project_id) "
        "VALUES (%s, %s, 'test-machine', 'claude', %s) "
        "ON CONFLICT (id) DO NOTHING",
        (session_id, session_id, _TEST_PROJECT_ID),
    )


def _insert_undelivered_message(db: HubDatabase, to_session: str) -> str:
    """Insert an undelivered inter-session message, returns message id."""
    _create_session(db, _SENDER_SESSION)
    _create_session(db, to_session)
    msg_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO inter_session_messages "
        "(id, from_session, to_session, content, priority, sent_at) "
        "VALUES (%s, %s, %s, 'hello', 'normal', CURRENT_TIMESTAMP)",
        (msg_id, _SENDER_SESSION, to_session),
    )
    return msg_id


def _insert_delivered_message(db: HubDatabase, to_session: str) -> str:
    """Insert an already-delivered inter-session message, returns message id."""
    _create_session(db, _SENDER_SESSION)
    _create_session(db, to_session)
    msg_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO inter_session_messages "
        "(id, from_session, to_session, content, priority, sent_at, delivered_at) "
        "VALUES (%s, %s, %s, 'hello', 'normal', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (msg_id, _SENDER_SESSION, to_session),
    )
    return msg_id


def _make_event_with_metadata(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
        metadata=metadata or {},
    )


def _notify_unread_mail_body() -> RuleDefinitionBody:
    """Build the notify-unread-mail rule body matching the YAML template."""
    return RuleDefinitionBody(
        event=RuleEvent.BEFORE_TOOL,
        agent_scope=["*"],
        when=(
            "has_pending_messages(event.metadata.get('_platform_session_id', '')) "
            "and not is_message_delivery_tool(event.data.get('mcp_tool'))"
        ),
        effects=[
            RuleEffect(
                type="inject_context",
                template=(
                    "You have {{ pending_message_count(event.metadata.get('_platform_session_id', '')) }}"
                    " undelivered inter-session message(s).\n"
                    "Please read them soon by calling: deliver_pending_messages(session_id=\"{{ event.metadata.get('_platform_session_id', '') }}\")"
                ),
            )
        ],
    )


# ═══════════════════════════════════════════════════════════════════════
# notify-unread-mail
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyUnreadMail:
    """notify-unread-mail injects context nudge when agent has pending messages."""

    @pytest.mark.asyncio
    async def test_injects_context_when_messages_pending(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        target_session = str(uuid.uuid4())
        _insert_undelivered_message(db, target_session)
        _insert_rule(manager, "notify-unread-mail", _notify_unread_mail_body(), priority=8)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
            metadata={"_platform_session_id": target_session},
        )
        variables: dict[str, Any] = {"_agent_type": "worker"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert "undelivered" in (response.context or "").lower()

    @pytest.mark.asyncio
    async def test_no_context_on_deliver_pending_messages(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """No nudge when the agent is already reading its mail."""
        target_session = str(uuid.uuid4())
        _insert_undelivered_message(db, target_session)
        _insert_rule(manager, "notify-unread-mail", _notify_unread_mail_body(), priority=8)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "mcp__gobby__call_tool", "mcp_tool": "deliver_pending_messages"},
            metadata={"_platform_session_id": target_session},
        )
        variables: dict[str, Any] = {"_agent_type": "worker"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert not response.context  # no nudge when already reading mail

    @pytest.mark.asyncio
    async def test_no_context_when_no_messages(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        target_session = str(uuid.uuid4())
        _create_session(db, target_session)
        _insert_rule(manager, "notify-unread-mail", _notify_unread_mail_body(), priority=8)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
            metadata={"_platform_session_id": target_session},
        )
        variables: dict[str, Any] = {"_agent_type": "worker"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert not response.context

    @pytest.mark.asyncio
    async def test_no_context_when_messages_already_delivered(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        target_session = str(uuid.uuid4())
        _insert_delivered_message(db, target_session)
        _insert_rule(manager, "notify-unread-mail", _notify_unread_mail_body(), priority=8)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
            metadata={"_platform_session_id": target_session},
        )
        variables: dict[str, Any] = {"_agent_type": "worker"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert not response.context

    @pytest.mark.asyncio
    async def test_skipped_for_root_sessions(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Root sessions (no _agent_type) should not get nudge from agent_scope: ['*']."""
        target_session = str(uuid.uuid4())
        _insert_undelivered_message(db, target_session)
        _insert_rule(manager, "notify-unread-mail", _notify_unread_mail_body(), priority=8)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
            metadata={"_platform_session_id": target_session},
        )
        # No _agent_type — this is a root session
        variables: dict[str, Any] = {}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert not response.context

    @pytest.mark.asyncio
    async def test_no_context_when_platform_session_id_absent(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Non-platform sessions (no _platform_session_id) get no nudge."""
        target_session = str(uuid.uuid4())
        _insert_undelivered_message(db, target_session)
        _insert_rule(manager, "notify-unread-mail", _notify_unread_mail_body(), priority=8)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
            metadata={},  # no _platform_session_id key
        )
        variables: dict[str, Any] = {"_agent_type": "worker"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert not response.context

    @pytest.mark.asyncio
    async def test_context_renders_message_count(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Injected context should include the count from pending_message_count."""
        target_session = str(uuid.uuid4())
        _insert_undelivered_message(db, target_session)
        _insert_undelivered_message(db, target_session)
        _insert_rule(manager, "notify-unread-mail", _notify_unread_mail_body(), priority=8)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
            metadata={"_platform_session_id": target_session},
        )
        variables: dict[str, Any] = {"_agent_type": "worker"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert "2 undelivered" in (response.context or "")


# ═══════════════════════════════════════════════════════════════════════
# Jinja2 helper rendering (template context includes allowed_funcs)
# ═══════════════════════════════════════════════════════════════════════


class TestJinja2HelperRendering:
    """Verify helper functions are accessible in Jinja2 templates."""

    @pytest.mark.asyncio
    async def test_pending_message_count_renders_in_block_reason(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """pending_message_count is callable from block reason templates."""
        target_session = str(uuid.uuid4())
        _insert_undelivered_message(db, target_session)
        _insert_undelivered_message(db, target_session)
        _insert_undelivered_message(db, target_session)

        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            agent_scope=["*"],
            when="has_pending_messages(event.metadata.get('_platform_session_id', ''))",
            effects=[
                RuleEffect(
                    type="block",
                    reason="Count: {{ pending_message_count(event.metadata.get('_platform_session_id', '')) }}",
                )
            ],
        )
        _insert_rule(manager, "count-test", body, priority=1)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
            metadata={"_platform_session_id": target_session},
        )
        variables: dict[str, Any] = {"_agent_type": "worker"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "Count: 3" in (response.reason or "")

    @pytest.mark.asyncio
    async def test_helpers_available_in_inject_context(
        self, db: HubDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Helper functions are accessible in inject_context templates."""
        target_session = str(uuid.uuid4())
        _insert_undelivered_message(db, target_session)

        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            agent_scope=["*"],
            when="has_pending_messages(event.metadata.get('_platform_session_id', ''))",
            effects=[
                RuleEffect(
                    type="inject_context",
                    template="Pending: {{ pending_message_count(event.metadata.get('_platform_session_id', '')) }}",
                )
            ],
        )
        _insert_rule(manager, "inject-test", body, priority=1)

        engine = RuleEngine(db)
        event = _make_event_with_metadata(
            HookEventType.BEFORE_TOOL,
            data={"tool_name": "Edit"},
            metadata={"_platform_session_id": target_session},
        )
        variables: dict[str, Any] = {"_agent_type": "worker"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        assert "Pending: 1" in (response.context or "")
