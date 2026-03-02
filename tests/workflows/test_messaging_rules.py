"""Tests for messaging rules.

Covers 5 rules:
- deliver-pending-messages: calls MCP on before_agent
- activate-pending-command: activates on before_agent when has_pending_command
- command-tool-restriction: blocks disallowed tools when command active
- command-exit-condition: auto-completes on after_tool when exit condition met
- notify-unread-mail: injects context nudge when agent has pending messages
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleEvent
from gobby.workflows.enforcement.blocking import is_message_delivery_tool
from gobby.workflows.rule_engine import RuleEngine

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_messaging_rules.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _make_event(
    event_type: HookEventType = HookEventType.BEFORE_TOOL,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="test-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data or {},
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


# ═══════════════════════════════════════════════════════════════════════
# deliver-pending-messages
# ═══════════════════════════════════════════════════════════════════════


class TestDeliverPendingMessages:
    """deliver-pending-messages calls MCP on before_agent for spawned agents only."""

    def _rule_body(self) -> RuleDefinitionBody:
        return RuleDefinitionBody(
            event=RuleEvent.BEFORE_AGENT,
            when="variables.get('is_spawned_agent')",
            effect=RuleEffect(
                type="mcp_call",
                server="gobby-agents",
                tool="deliver_pending_messages",
            ),
        )

    @pytest.mark.asyncio
    async def test_fires_on_before_agent(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "deliver-pending-messages", self._rule_body(), priority=10)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        variables: dict[str, Any] = {"is_spawned_agent": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 1

    @pytest.mark.asyncio
    async def test_records_correct_mcp_call(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "deliver-pending-messages", self._rule_body(), priority=10)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        variables: dict[str, Any] = {"is_spawned_agent": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        call = response.metadata["mcp_calls"][0]
        assert call["server"] == "gobby-agents"
        assert call["tool"] == "deliver_pending_messages"

    @pytest.mark.asyncio
    async def test_skips_for_non_agent_session(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "deliver-pending-messages", self._rule_body(), priority=10)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        response = await engine.evaluate(event, session_id="sess-1", variables={})

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# activate-pending-command
# ═══════════════════════════════════════════════════════════════════════


class TestActivatePendingCommand:
    """activate-pending-command fires when has_pending_command is set on spawned agents."""

    def _rule_body(self) -> RuleDefinitionBody:
        return RuleDefinitionBody(
            event=RuleEvent.BEFORE_AGENT,
            when="variables.get('is_spawned_agent') and variables.get('has_pending_command')",
            effect=RuleEffect(
                type="mcp_call",
                server="gobby-agents",
                tool="activate_command",
            ),
        )

    @pytest.mark.asyncio
    async def test_fires_when_has_pending_command(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "activate-pending-command", self._rule_body(), priority=15)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        variables: dict[str, Any] = {"is_spawned_agent": True, "has_pending_command": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 1
        assert mcp_calls[0]["tool"] == "activate_command"

    @pytest.mark.asyncio
    async def test_skips_when_no_pending_command(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "activate-pending-command", self._rule_body(), priority=15)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        variables: dict[str, Any] = {"is_spawned_agent": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 0

    @pytest.mark.asyncio
    async def test_skips_for_non_agent_session(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "activate-pending-command", self._rule_body(), priority=15)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_AGENT)
        variables: dict[str, Any] = {"has_pending_command": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# command-tool-restriction
# ═══════════════════════════════════════════════════════════════════════


class TestCommandToolRestriction:
    """command-tool-restriction blocks disallowed tools when command active on spawned agents."""

    def _rule_body(self) -> RuleDefinitionBody:
        return RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            when=(
                "variables.get('is_spawned_agent') "
                "and variables.get('command_id') "
                "and variables.get('allowed_tools') "
                "and event.data.get('tool_name') not in variables.get('allowed_tools', [])"
            ),
            effect=RuleEffect(
                type="block",
                reason="Tool not allowed by active command",
            ),
        )

    @pytest.mark.asyncio
    async def test_blocks_disallowed_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-tool-restriction", self._rule_body(), priority=5)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {
            "is_spawned_agent": True,
            "command_id": "cmd-1",
            "allowed_tools": ["Read", "Grep"],
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "not allowed" in (response.reason or "").lower()

    @pytest.mark.asyncio
    async def test_allows_permitted_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-tool-restriction", self._rule_body(), priority=5)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Read"})
        variables: dict[str, Any] = {
            "is_spawned_agent": True,
            "command_id": "cmd-1",
            "allowed_tools": ["Read", "Grep"],
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_no_restriction_without_command(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-tool-restriction", self._rule_body(), priority=5)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {"is_spawned_agent": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_no_restriction_for_non_agent_session(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Non-agent sessions should never be blocked, even with command_id and allowed_tools."""
        _insert_rule(manager, "command-tool-restriction", self._rule_body(), priority=5)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.BEFORE_TOOL, data={"tool_name": "Write"})
        variables: dict[str, Any] = {
            "command_id": "cmd-1",
            "allowed_tools": ["Read", "Grep"],
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"


# ═══════════════════════════════════════════════════════════════════════
# command-mcp-tool-restriction
# ═══════════════════════════════════════════════════════════════════════


class TestCommandMcpToolRestriction:
    """command-mcp-tool-restriction blocks disallowed MCP tools on spawned agents."""

    def _rule_body(self) -> RuleDefinitionBody:
        return RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            when=(
                "variables.get('is_spawned_agent') "
                "and variables.get('command_id') "
                "and variables.get('allowed_mcp_tools') "
                "and event.data.get('mcp_server') "
                "and event.data.get('mcp_tool') "
                "and (event.data.get('mcp_server') + ':' + event.data.get('mcp_tool')) "
                "not in variables.get('allowed_mcp_tools', [])"
            ),
            effect=RuleEffect(
                type="block",
                reason="MCP tool not allowed by active command",
            ),
        )

    @pytest.mark.asyncio
    async def test_blocks_disallowed_mcp_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-mcp-tool-restriction", self._rule_body(), priority=5)

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"mcp_server": "gobby-tasks", "mcp_tool": "delete_task"},
        )
        variables: dict[str, Any] = {
            "is_spawned_agent": True,
            "command_id": "cmd-1",
            "allowed_mcp_tools": ["gobby-tasks:get_task", "gobby-tasks:list_tasks"],
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "block"
        assert "not allowed" in (response.reason or "").lower()

    @pytest.mark.asyncio
    async def test_allows_permitted_mcp_tool(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-mcp-tool-restriction", self._rule_body(), priority=5)

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"mcp_server": "gobby-tasks", "mcp_tool": "get_task"},
        )
        variables: dict[str, Any] = {
            "is_spawned_agent": True,
            "command_id": "cmd-1",
            "allowed_mcp_tools": ["gobby-tasks:get_task", "gobby-tasks:list_tasks"],
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_no_restriction_for_non_agent_session(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Non-agent sessions should never be blocked by MCP tool restrictions."""
        _insert_rule(manager, "command-mcp-tool-restriction", self._rule_body(), priority=5)

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"mcp_server": "gobby-tasks", "mcp_tool": "delete_task"},
        )
        variables: dict[str, Any] = {
            "command_id": "cmd-1",
            "allowed_mcp_tools": ["gobby-tasks:get_task"],
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_no_restriction_without_command(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-mcp-tool-restriction", self._rule_body(), priority=5)

        engine = RuleEngine(db)
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"mcp_server": "gobby-tasks", "mcp_tool": "delete_task"},
        )
        variables: dict[str, Any] = {"is_spawned_agent": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"

    @pytest.mark.asyncio
    async def test_yaml_loaded_expression_parses_correctly(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Verify the expression from the YAML template parses without SyntaxError.

        Regression test: YAML `>` folded scalar preserved a newline before
        'not in' due to extra indentation, causing ast.parse to fail with
        SyntaxError. Block rules fail-closed on errors, so the rule blocked
        ALL tools unconditionally.
        """
        import yaml

        yaml_path = "src/gobby/install/shared/rules/messaging/command-tool-restriction.yaml"
        with open(yaml_path) as f:
            data = yaml.safe_load(f)

        when_expr = data["rules"]["command-mcp-tool-restriction"]["when"]

        # The expression must parse as a single eval expression
        import ast

        tree = ast.parse(when_expr, mode="eval")
        assert isinstance(tree, ast.Expression)

        # Also verify it evaluates correctly through the rule engine
        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            when=when_expr,
            effect=RuleEffect(type="block", reason="MCP tool not allowed"),
        )
        _insert_rule(manager, "yaml-loaded-mcp-restriction", body, priority=5)

        engine = RuleEngine(db)

        # Non-agent session should NOT be blocked
        event = _make_event(
            HookEventType.BEFORE_TOOL,
            data={"mcp_server": "gobby-tasks", "mcp_tool": "delete_task"},
        )
        response = await engine.evaluate(event, session_id="sess-1", variables={})
        assert response.decision == "allow"

        # Agent session with disallowed tool SHOULD be blocked
        variables: dict[str, Any] = {
            "is_spawned_agent": True,
            "command_id": "cmd-1",
            "allowed_mcp_tools": ["gobby-tasks:get_task"],
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)
        assert response.decision == "block"


# ═══════════════════════════════════════════════════════════════════════
# command-exit-condition
# ═══════════════════════════════════════════════════════════════════════


class TestCommandExitCondition:
    """command-exit-condition auto-completes when exit condition met on spawned agents."""

    def _rule_body(self) -> RuleDefinitionBody:
        return RuleDefinitionBody(
            event=RuleEvent.AFTER_TOOL,
            when=(
                "variables.get('is_spawned_agent') "
                "and variables.get('command_id') "
                "and variables.get('exit_condition_met')"
            ),
            effect=RuleEffect(
                type="mcp_call",
                server="gobby-agents",
                tool="complete_command",
            ),
        )

    @pytest.mark.asyncio
    async def test_fires_when_exit_condition_met(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-exit-condition", self._rule_body(), priority=90)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.AFTER_TOOL)
        variables: dict[str, Any] = {
            "is_spawned_agent": True,
            "command_id": "cmd-1",
            "exit_condition_met": True,
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 1
        assert mcp_calls[0]["tool"] == "complete_command"

    @pytest.mark.asyncio
    async def test_skips_when_exit_not_met(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-exit-condition", self._rule_body(), priority=90)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.AFTER_TOOL)
        variables: dict[str, Any] = {"is_spawned_agent": True, "command_id": "cmd-1"}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        assert response.decision == "allow"
        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 0

    @pytest.mark.asyncio
    async def test_skips_without_command(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        _insert_rule(manager, "command-exit-condition", self._rule_body(), priority=90)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.AFTER_TOOL)
        variables: dict[str, Any] = {"is_spawned_agent": True, "exit_condition_met": True}
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

        mcp_calls = response.metadata.get("mcp_calls", [])
        assert len(mcp_calls) == 0

    @pytest.mark.asyncio
    async def test_skips_for_non_agent_session(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Non-agent sessions should not trigger command exit, even with all other vars set."""
        _insert_rule(manager, "command-exit-condition", self._rule_body(), priority=90)

        engine = RuleEngine(db)
        event = _make_event(HookEventType.AFTER_TOOL)
        variables: dict[str, Any] = {
            "command_id": "cmd-1",
            "exit_condition_met": True,
        }
        response = await engine.evaluate(event, session_id="sess-1", variables=variables)

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


def _ensure_project(db: LocalDatabase) -> None:
    """Insert a minimal project row so session FK constraints are satisfied."""
    db.execute(
        "INSERT OR IGNORE INTO projects (id, name) VALUES (?, 'test-project')",
        (_TEST_PROJECT_ID,),
    )


def _create_session(db: LocalDatabase, session_id: str) -> None:
    """Insert a minimal session row so FK constraints are satisfied."""
    _ensure_project(db)
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, external_id, machine_id, source, project_id) "
        "VALUES (?, ?, 'test-machine', 'claude', ?)",
        (session_id, session_id, _TEST_PROJECT_ID),
    )


def _insert_undelivered_message(db: LocalDatabase, to_session: str) -> str:
    """Insert an undelivered inter-session message, returns message id."""
    _create_session(db, _SENDER_SESSION)
    _create_session(db, to_session)
    msg_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO inter_session_messages "
        "(id, from_session, to_session, content, priority, sent_at) "
        "VALUES (?, ?, ?, 'hello', 'normal', datetime('now'))",
        (msg_id, _SENDER_SESSION, to_session),
    )
    return msg_id


def _insert_delivered_message(db: LocalDatabase, to_session: str) -> str:
    """Insert an already-delivered inter-session message, returns message id."""
    _create_session(db, _SENDER_SESSION)
    _create_session(db, to_session)
    msg_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO inter_session_messages "
        "(id, from_session, to_session, content, priority, sent_at, delivered_at) "
        "VALUES (?, ?, ?, 'hello', 'normal', datetime('now'), datetime('now'))",
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
        effect=RuleEffect(
            type="inject_context",
            template=(
                "You have {{ pending_message_count(event.metadata.get('_platform_session_id', '')) }}"
                " undelivered inter-session message(s).\n"
                "Please read them soon by calling: deliver_pending_messages(session_id=\"{{ event.metadata.get('_platform_session_id', '') }}\")"
            ),
        ),
    )


# ═══════════════════════════════════════════════════════════════════════
# notify-unread-mail
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyUnreadMail:
    """notify-unread-mail injects context nudge when agent has pending messages."""

    @pytest.mark.asyncio
    async def test_injects_context_when_messages_pending(
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
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
            effect=RuleEffect(
                type="block",
                reason="Count: {{ pending_message_count(event.metadata.get('_platform_session_id', '')) }}",
            ),
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
        self, db: LocalDatabase, manager: LocalWorkflowDefinitionManager
    ) -> None:
        """Helper functions are accessible in inject_context templates."""
        target_session = str(uuid.uuid4())
        _insert_undelivered_message(db, target_session)

        body = RuleDefinitionBody(
            event=RuleEvent.BEFORE_TOOL,
            agent_scope=["*"],
            when="has_pending_messages(event.metadata.get('_platform_session_id', ''))",
            effect=RuleEffect(
                type="inject_context",
                template="Pending: {{ pending_message_count(event.metadata.get('_platform_session_id', '')) }}",
            ),
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
