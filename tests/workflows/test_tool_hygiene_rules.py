"""Tests for bundled tool-hygiene rules."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.definitions import AgentSelector, RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules
from tests.fixtures.agent_definitions import make_agent_definition, make_agent_workflows

pytestmark = pytest.mark.unit

# Session id columns are native uuid in PostgreSQL; synthetic ids like
# SESSION_ID would fail with `invalid input syntax for type uuid`.
SESSION_ID = "11111111-1111-4111-8111-111111111111"

CLAUDE_MEMORY_RULES = {
    "block-claude-memory-read",
    "block-claude-memory-search",
    "block-claude-memory-tool",
    "block-claude-memory-write",
}


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> None:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    sync_bundled_rules(db, get_bundled_rules_path())


class TestToolHygieneSync:
    """Test that tool-hygiene.yaml syncs correctly."""

    def test_bundled_file_syncs_target_rules(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Key tool-hygiene rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        assert "block-escaped-quotes" not in rule_names
        assert "require-uv" in rule_names
        assert CLAUDE_MEMORY_RULES.issubset(rule_names)

    def test_all_rules_have_group(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        """All tool-hygiene rules should have group='tool-hygiene'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in {"require-uv"} | CLAUDE_MEMORY_RULES:
                body = row.definition_json
                assert body.get("group") == "tool-hygiene", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in {"require-uv"} | CLAUDE_MEMORY_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                effect_types = {e.type for e in body.resolved_effects}
                assert effect_types <= {"block", "set_variable", "rewrite_input", "inject_context"}

    def test_deprecated_block_escaped_quotes_rule_is_orphaned(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """Bundled sync soft-deletes the retired block-escaped-quotes rule."""
        body = RuleDefinitionBody(
            event="before_tool",
            effects=[RuleEffect(type="block", reason="retired rule")],
            group="tool-hygiene",
        )
        manager.create(
            name="block-escaped-quotes",
            definition_json=body.model_dump_json(),
            enabled=True,
            priority=20,
            tags=["tool-hygiene", "gobby"],
            source="installed",
        )

        _sync_bundled(db)

        assert manager.get_by_name("block-escaped-quotes") is None
        deleted = manager.get_by_name("block-escaped-quotes", include_deleted=True)
        assert deleted is not None
        assert deleted.deleted_at is not None


REQUIRE_UV_REASON = (
    "Use `uv pip …` or `uv run python -m pip …` — uv manages this project's Python environment."
)
REQUIRE_UV_COMMAND_PATTERN = (
    r"(^|(?<=[;&|]))\s*(?:sudo\s+)?"
    r"(?:pip3?\b|python(?:\d+(?:\.\d+)?)?\s+-m\s+pip\b)"
)


class TestRequireUvRule:
    """Verify require-uv guards Python package management commands."""

    def test_uses_single_block_effect(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """require-uv should only block matching Bash commands."""
        _sync_bundled(db)

        row = manager.get_by_name("require-uv")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert row.description == "Require uv for Python package management"
        assert len(body.resolved_effects) == 1

        effect = body.resolved_effects[0]
        assert effect.type == "block"
        assert effect.tools == ["Bash"]
        assert effect.command_pattern == REQUIRE_UV_COMMAND_PATTERN
        assert effect.reason == REQUIRE_UV_REASON

    def test_has_no_rewrite_or_context_effects(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """require-uv should not return modified_input through rewrite effects."""
        _sync_bundled(db)

        row = manager.get_by_name("require-uv")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        effect_types = {e.type for e in body.resolved_effects}
        assert "rewrite_input" not in effect_types
        assert "inject_context" not in effect_types

    def test_has_when_condition(self, db: HubDatabase, manager: RuleDefinitionManager) -> None:
        """require-uv should only fire when require_uv variable is set."""
        _sync_bundled(db)

        row = manager.get_by_name("require-uv")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)

        assert body.when is not None
        assert "require_uv" in body.when

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "python script.py",
            'python -c "print(1)"',
            "python -m http.server",
            'uv run python -c "print(1)"',
            "uv pip install requests",
            "uv run python -m pip install requests",
        ],
    )
    async def test_bundled_rule_allows_non_package_and_uv_managed_commands(
        self, db: HubDatabase, manager: RuleDefinitionManager, command: str
    ) -> None:
        """Ordinary Python and uv-managed package commands should pass."""
        _sync_bundled(db)

        event = _make_bash_event(command, source=SessionSource.CODEX)
        engine = RuleEngine(db)

        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": True}
        )

        assert response.decision == "allow"
        assert response.modified_input is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            "pip install requests",
            "pip3 install requests",
            "python -m pip install requests",
            "python3.13 -m pip install requests",
        ],
    )
    async def test_bundled_rule_blocks_unmanaged_package_commands_without_rewrite(
        self, db: HubDatabase, manager: RuleDefinitionManager, command: str
    ) -> None:
        """Unmanaged package commands should block without a rewrite payload."""
        _sync_bundled(db)

        event = _make_bash_event(command, source=SessionSource.CODEX)
        engine = RuleEngine(db)

        response = await engine.evaluate(
            event, session_id=SESSION_ID, variables={"require_uv": True}
        )

        assert response.decision == "block"
        assert response.reason == f"Rule enforced by Gobby: [require-uv]\n{REQUIRE_UV_REASON}"
        assert response.modified_input is None
        assert response.auto_approve is False


def _make_normalized_bash_event(command: str) -> HookEvent:
    data: dict[str, object] = {"tool_name": "Bash", "tool_input": {"command": command}}
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
    )


class TestClaudeMemoryHygieneRules:
    """Verify Claude file-memory hygiene uses canonical kind/path metadata."""

    def test_block_effects_include_bash(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        expected_tools = {
            "block-claude-memory-read": ["Read", "Bash"],
            "block-claude-memory-search": ["Glob", "Grep", "Bash"],
            "block-claude-memory-write": ["Write", "Edit", "Bash"],
        }

        for rule_name, tools in expected_tools.items():
            row = manager.get_by_name(rule_name)
            assert row is not None
            body = RuleDefinitionBody.model_validate(row.definition_json)
            assert body.when is not None
            assert "canonical_tool_kind" in body.when
            assert "touches_claude_memory_path" in body.when
            assert body.resolved_effects[0].tools == tools

    def test_native_memory_tool_rule_structure(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        """The harness-level Memory tool is blocked by name, not by path."""
        _sync_bundled(db)
        row = manager.get_by_name("block-claude-memory-tool")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "'Memory'" in body.when
        assert body.resolved_effects[0].tools == ["Memory"]

    @pytest.mark.asyncio
    async def test_blocks_native_memory_tool(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        data: dict[str, object] = {
            "tool_name": "Memory",
            "tool_input": {"command": "view", "path": "/memories"},
        }
        normalize_tool_fields(data)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data=data,
        )

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "Use gobby-memory" in response.reason

    @pytest.mark.asyncio
    async def test_blocks_shell_read_workaround(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        event = _make_normalized_bash_event("cat .claude/memory/project.md")

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "Use gobby-memory" in response.reason

    @pytest.mark.asyncio
    async def test_blocks_shell_search_workaround(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        event = _make_normalized_bash_event("rg project .claude/memory")

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "Use gobby-memory" in response.reason

    @pytest.mark.asyncio
    async def test_blocks_shell_write_workaround(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        event = _make_normalized_bash_event("printf hello > .claude/memory/project.md")

        response = await RuleEngine(db).evaluate(
            event,
            session_id=SESSION_ID,
            variables={"task_claimed": True},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert "Use gobby-memory" in response.reason


def _make_bash_event(command: str, source: SessionSource = SessionSource.CLAUDE) -> HookEvent:
    """Create a before_tool HookEvent with command nested in tool_input (like real adapters)."""
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Bash", "tool_input": {"command": command}},
    )


def _make_shell_alias_event(tool_name: str, command: str) -> HookEvent:
    """Create a before_tool HookEvent for shell aliases."""
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"tool_name": tool_name, "tool_input": {"command": command}},
    )


def _require_uv_effect() -> RuleEffect:
    """Build the RuleEffect matching the require-uv rule definition."""
    return RuleEffect(
        type="block",
        tools=["Bash"],
        command_pattern=REQUIRE_UV_COMMAND_PATTERN,
        reason=REQUIRE_UV_REASON,
        resolve_uv_run=False,
    )


class TestRequireUvShouldBlock:
    """Integration tests for _should_block with realistic adapter event data.

    All adapters nest the command inside tool_input.command, not at the
    top level of event.data. These tests verify the extraction works.
    """

    @pytest.mark.parametrize(
        "command",
        ["python script.py", 'python -c "print(1)"', "python3 -m http.server"],
    )
    def test_allows_bare_python(self, db: HubDatabase, command: str) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event(command)
        assert engine._should_block(_require_uv_effect(), event) is False

    @pytest.mark.parametrize(
        "command",
        [
            "pip install requests",
            "pip3 install requests",
            "python -m pip install requests",
            "python3 -m pip install requests",
            "python3.13 -m pip install requests",
        ],
    )
    def test_blocks_unmanaged_package_commands(self, db: HubDatabase, command: str) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event(command)
        assert engine._should_block(_require_uv_effect(), event) is True

    @pytest.mark.parametrize(
        "command",
        [
            'uv run python -c "print(1)"',
            "uv run pytest tests/ -v",
            "uv pip install requests",
            "uv run python -m pip install requests",
            "uv -q run --with pyyaml python -m pip install requests",
        ],
    )
    def test_allows_uv_managed_commands(self, db: HubDatabase, command: str) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event(command)
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_allows_non_python_command(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("ls -la")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_allows_python_after_chain(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("cd /tmp && python test.py")
        assert engine._should_block(_require_uv_effect(), event) is False

    @pytest.mark.parametrize(
        "command",
        [
            "cd /tmp && pip install x",
            "echo ready; pip3 install x",
            "printf archive | python3.13 -m pip install x",
        ],
    )
    def test_blocks_package_management_after_separator(self, db: HubDatabase, command: str) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event(command)
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_allows_uv_run_python_after_chain(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("cd /tmp && uv run python test.py")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_blocks_package_command_at_top_level(self, db: HubDatabase) -> None:
        """Legacy path: command at top level of event.data still works."""
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Bash", "command": "python -m pip install x"},
        )
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_allows_bare_python_through_normalized_exec_command(self, db: HubDatabase) -> None:
        engine = RuleEngine(db)
        event = _make_shell_alias_event("exec_command", "python script.py")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_blocks_package_management_through_normalized_exec_command(
        self, db: HubDatabase
    ) -> None:
        engine = RuleEngine(db)
        event = _make_shell_alias_event("exec_command", "python3.13 -m pip install x")
        assert engine._should_block(_require_uv_effect(), event) is True


# The terminal tools the send_keys scope rules cover, each with the arguments it takes.
TERMINAL_TOOL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "send_keys": {"keys": "ls"},
    "capture_output": {"lines": 50},
}
TERMINAL_TOOL_MATCHERS = ["gobby-sessions:send_keys", "gobby-sessions:capture_output"]


class TestBlockWebChatSendKeys:
    def test_rule_syncs_enabled_with_terminal_tool_matchers(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("block-web-chat-send-keys")
        assert row is not None
        assert row.enabled is True
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when == "event.metadata.get('session_type') == 'web_chat'"
        assert body.resolved_effects[0].mcp_tools == TERMINAL_TOOL_MATCHERS

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", list(TERMINAL_TOOL_ARGUMENTS))
    @pytest.mark.parametrize(
        ("session_type", "expected_decision"),
        [("web_chat", "block"), ("terminal", "allow")],
    )
    async def test_rule_applies_only_to_web_chat(
        self,
        db: HubDatabase,
        tool: str,
        session_type: str,
        expected_decision: str,
    ) -> None:
        _sync_bundled(db)
        data: dict[str, object] = {
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-sessions",
                "tool_name": tool,
                "arguments": {"session_id": "#42", **TERMINAL_TOOL_ARGUMENTS[tool]},
            },
        }
        normalize_tool_fields(data)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data=data,
            metadata={"session_type": session_type},
        )

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == expected_decision
        if expected_decision == "block":
            assert response.reason is not None
            assert "block-web-chat-send-keys" in response.reason


CROSS_PROJECT_SEND_KEYS = "block-cross-project-send-keys"


class _UnreachableSessions(SessionManager):
    """A session store whose lookups fail the way an unavailable hub does."""

    def resolve_session_reference(self, ref: str, project_id: str | None = None) -> str:
        raise RuntimeError("session store unavailable")


def _terminal_tool_event(caller_id: str, target_ref: str, tool: str) -> HookEvent:
    """The before_tool event the MCP proxy builds for one terminal-tool dispatch."""
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-sessions",
            "tool_name": tool,
            "arguments": {"session_id": target_ref, **TERMINAL_TOOL_ARGUMENTS[tool]},
        },
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=caller_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": caller_id, "_mcp_proxy_dispatch": True},
    )


class TestBlockCrossProjectSendKeys:
    @pytest.fixture
    def tree(
        self,
        session_manager: SessionManager,
        project_manager: LocalProjectManager,
        sample_project: dict[str, Any],
    ) -> dict[str, Session]:
        """A caller with a cross-project parent and child, a project peer and an outsider."""
        home = str(sample_project["id"])
        away = project_manager.create(name="send-keys-elsewhere").id

        def register(name: str, project_id: str, parent: Session | None = None) -> Session:
            return session_manager.register(
                external_id=f"send-keys-{name}",
                machine_id=require_machine_id(),
                source="claude",
                project_id=project_id,
                parent_session_id=None if parent is None else parent.id,
            )

        ancestor = register("ancestor", away)
        caller = register("caller", home, ancestor)
        return {
            "caller": caller,
            "own": caller,
            "same_project": register("peer", home),
            "ancestor": ancestor,
            "descendant": register("descendant", away, caller),
            "cross_project": register("outsider", away),
        }

    def test_rule_syncs_enabled_with_send_message_redirect(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)

        row = manager.get_by_name(CROSS_PROJECT_SEND_KEYS)
        assert row is not None
        assert row.enabled is True
        effect = RuleDefinitionBody.model_validate(row.definition_json).resolved_effects[0]
        assert effect.type == "block"
        assert effect.mcp_tools == TERMINAL_TOOL_MATCHERS
        assert effect.reason is not None
        assert "gobby-agents:send_message" in effect.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", list(TERMINAL_TOOL_ARGUMENTS))
    @pytest.mark.parametrize(
        ("target", "expected_decision"),
        [
            ("own", "allow"),
            ("same_project", "allow"),
            ("ancestor", "allow"),
            ("descendant", "allow"),
            ("cross_project", "block"),
        ],
    )
    async def test_rule_admits_only_the_callers_project_and_agent_tree(
        self,
        db: HubDatabase,
        session_manager: SessionManager,
        tree: dict[str, Session],
        tool: str,
        target: str,
        expected_decision: str,
    ) -> None:
        _sync_bundled(db)
        caller_id = tree["caller"].id
        engine = RuleEngine(db, session_manager=session_manager)

        response = await engine.evaluate(
            _terminal_tool_event(caller_id, tree[target].id, tool),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == expected_decision
        if expected_decision == "block":
            assert response.reason is not None
            assert CROSS_PROJECT_SEND_KEYS in response.reason
            assert "gobby-agents:send_message" in response.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", list(TERMINAL_TOOL_ARGUMENTS))
    async def test_disabling_the_rule_lifts_the_cross_project_block(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
        session_manager: SessionManager,
        tree: dict[str, Session],
        tool: str,
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name(CROSS_PROJECT_SEND_KEYS)
        assert row is not None
        manager.update(row.id, enabled=False)
        caller_id = tree["caller"].id

        response = await RuleEngine(db, session_manager=session_manager).evaluate(
            _terminal_tool_event(caller_id, tree["cross_project"].id, tool),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == "allow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", list(TERMINAL_TOOL_ARGUMENTS))
    async def test_evaluation_error_still_refuses_the_cross_project_target(
        self, db: HubDatabase, tree: dict[str, Session], tool: str
    ) -> None:
        _sync_bundled(db)
        caller_id = tree["caller"].id
        engine = RuleEngine(db, session_manager=_UnreachableSessions(db))

        response = await engine.evaluate(
            _terminal_tool_event(caller_id, tree["cross_project"].id, tool),
            session_id=caller_id,
            variables={},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert CROSS_PROJECT_SEND_KEYS in response.reason


SEND_MESSAGE_MODES = "scope-spawned-agent-send-message"
ALL_MODES = ("parent", "session", "agent", "project", "global", "build")


def _send_message_event(caller_id: str, target: str | None) -> HookEvent:
    """The before_tool event the MCP proxy builds for one send_message dispatch."""
    arguments: dict[str, Any] = {"content": "status"}
    if target is not None:
        arguments["target"] = target
    data: dict[str, Any] = {
        "tool_name": "mcp__gobby__call_tool",
        "tool_input": {
            "server_name": "gobby-agents",
            "tool_name": "send_message",
            "arguments": arguments,
        },
    }
    normalize_tool_fields(data)
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=caller_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata={"_platform_session_id": caller_id, "_mcp_proxy_dispatch": True},
    )


class TestScopeSpawnedAgentSendMessage:
    @pytest.fixture
    def sessions(
        self, db: HubDatabase, session_manager: SessionManager, sample_project: dict[str, Any]
    ) -> dict[str, Session]:
        """A root session with one spawned child, plus the child's two candidate definitions."""
        project_id = str(sample_project["id"])
        for name, extra in (
            ("quiet-worker", {}),
            ("announcer", {"send_message_targets": ["parent", "project"]}),
        ):
            body = make_agent_definition(
                name=name,
                prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                workflows=make_agent_workflows(
                    rule_selectors=AgentSelector(include=["tag:default"])
                ),
                **extra,
            )
            AgentDefinitionManager(db).create(
                name=name, definition_json=body.model_dump(), source="custom"
            )
        root = session_manager.register(
            external_id="send-message-root",
            machine_id=require_machine_id(),
            source="claude",
            project_id=project_id,
        )
        child = session_manager.register(
            external_id="send-message-child",
            machine_id=require_machine_id(),
            source="claude",
            project_id=project_id,
            parent_session_id=root.id,
            agent_depth=1,
        )
        return {"root": root, "child": child}

    async def _decide(
        self,
        db: HubDatabase,
        session_manager: SessionManager,
        caller: Session,
        agent_type: str | None,
        target: str | None,
    ) -> tuple[str, str | None]:
        variables = {} if agent_type is None else {"_agent_type": agent_type}
        response = await RuleEngine(db, session_manager=session_manager).evaluate(
            _send_message_event(caller.id, target), session_id=caller.id, variables=variables
        )
        return response.decision, response.reason

    def test_rule_syncs_enabled_for_send_message(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)

        row = manager.get_by_name(SEND_MESSAGE_MODES)
        assert row is not None
        assert row.enabled is True
        assert "default" in (row.tags or [])
        effect = RuleDefinitionBody.model_validate(row.definition_json).resolved_effects[0]
        assert effect.type == "block"
        assert effect.mcp_tools == ["gobby-agents:send_message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", [None, "parent"])
    async def test_undeclared_agent_reaches_its_parent(
        self,
        db: HubDatabase,
        session_manager: SessionManager,
        sessions: dict[str, Session],
        target: str | None,
    ) -> None:
        _sync_bundled(db)

        decision, _ = await self._decide(
            db, session_manager, sessions["child"], "quiet-worker", target
        )

        assert decision == "allow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ["session", "agent", "project", "global", "build"])
    async def test_undeclared_agent_is_refused_every_other_mode(
        self,
        db: HubDatabase,
        session_manager: SessionManager,
        sessions: dict[str, Session],
        target: str,
    ) -> None:
        _sync_bundled(db)

        decision, reason = await self._decide(
            db, session_manager, sessions["child"], "quiet-worker", target
        )

        assert decision == "block"
        assert reason is not None
        assert SEND_MESSAGE_MODES in reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("target", "expected_decision"),
        [("parent", "allow"), ("project", "allow"), ("global", "block"), ("session", "block")],
    )
    async def test_declared_modes_widen_the_agent(
        self,
        db: HubDatabase,
        session_manager: SessionManager,
        sessions: dict[str, Session],
        target: str,
        expected_decision: str,
    ) -> None:
        _sync_bundled(db)

        decision, _ = await self._decide(
            db, session_manager, sessions["child"], "announcer", target
        )

        assert decision == expected_decision

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ALL_MODES)
    async def test_root_session_is_unrestricted(
        self,
        db: HubDatabase,
        session_manager: SessionManager,
        sessions: dict[str, Session],
        target: str,
    ) -> None:
        _sync_bundled(db)

        decision, _ = await self._decide(db, session_manager, sessions["root"], None, target)

        assert decision == "allow"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_type", [None, "unregistered-agent"])
    async def test_unresolvable_definition_refuses_even_parent(
        self,
        db: HubDatabase,
        session_manager: SessionManager,
        sessions: dict[str, Session],
        agent_type: str | None,
    ) -> None:
        _sync_bundled(db)

        decision, reason = await self._decide(
            db, session_manager, sessions["child"], agent_type, "parent"
        )

        assert decision == "block"
        assert reason is not None
        assert SEND_MESSAGE_MODES in reason

    @pytest.mark.asyncio
    async def test_unresolvable_lineage_refuses_even_parent(
        self, db: HubDatabase, sessions: dict[str, Session]
    ) -> None:
        _sync_bundled(db)
        caller_id = sessions["child"].id

        response = await RuleEngine(db, session_manager=_UnreachableSessions(db)).evaluate(
            _send_message_event(caller_id, "parent"),
            session_id=caller_id,
            variables={"_agent_type": "quiet-worker"},
        )

        assert response.decision == "block"
        assert response.reason is not None
        assert SEND_MESSAGE_MODES in response.reason

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", ALL_MODES)
    async def test_disabling_the_rule_lifts_every_mode(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
        session_manager: SessionManager,
        sessions: dict[str, Session],
        target: str,
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name(SEND_MESSAGE_MODES)
        assert row is not None
        manager.update(row.id, enabled=False)

        decision, _ = await self._decide(
            db, session_manager, sessions["child"], "quiet-worker", target
        )

        assert decision == "allow"
