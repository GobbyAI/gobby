"""Tests for bundled tool-hygiene rules."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

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


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    return sync_bundled_rules(db, get_bundled_rules_path())


class TestToolHygieneSync:
    """Test that tool-hygiene.yaml syncs correctly."""

    def test_bundled_file_syncs_target_rules(self, db, manager) -> None:
        """Key tool-hygiene rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        assert "block-escaped-quotes" not in rule_names
        assert "require-uv" in rule_names
        assert CLAUDE_MEMORY_RULES.issubset(rule_names)

    def test_all_rules_have_group(self, db, manager) -> None:
        """All tool-hygiene rules should have group='tool-hygiene'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in {"require-uv"} | CLAUDE_MEMORY_RULES:
                body = row.definition_json
                assert body.get("group") == "tool-hygiene", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in {"require-uv"} | CLAUDE_MEMORY_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
                effect_types = {e.type for e in body.resolved_effects}
                assert effect_types <= {"block", "set_variable", "rewrite_input", "inject_context"}

    def test_deprecated_block_escaped_quotes_rule_is_orphaned(self, db, manager) -> None:
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

    def test_uses_single_block_effect(self, db, manager) -> None:
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

    def test_has_no_rewrite_or_context_effects(self, db, manager) -> None:
        """require-uv should not return modified_input through rewrite effects."""
        _sync_bundled(db)

        row = manager.get_by_name("require-uv")
        body = RuleDefinitionBody.model_validate(row.definition_json)

        effect_types = {e.type for e in body.resolved_effects}
        assert "rewrite_input" not in effect_types
        assert "inject_context" not in effect_types

    def test_has_when_condition(self, db, manager) -> None:
        """require-uv should only fire when require_uv variable is set."""
        _sync_bundled(db)

        row = manager.get_by_name("require-uv")
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
        self, db, manager, command: str
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
        self, db, manager, command: str
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

    def test_block_effects_include_bash(self, db, manager) -> None:
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

    def test_native_memory_tool_rule_structure(self, db, manager) -> None:
        """The harness-level Memory tool is blocked by name, not by path."""
        _sync_bundled(db)
        row = manager.get_by_name("block-claude-memory-tool")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "'Memory'" in body.when
        assert body.resolved_effects[0].tools == ["Memory"]

    @pytest.mark.asyncio
    async def test_blocks_native_memory_tool(self, db) -> None:
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
    async def test_blocks_shell_read_workaround(self, db) -> None:
        _sync_bundled(db)
        event = _make_normalized_bash_event("cat .claude/memory/project.md")

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "Use gobby-memory" in response.reason

    @pytest.mark.asyncio
    async def test_blocks_shell_search_workaround(self, db) -> None:
        _sync_bundled(db)
        event = _make_normalized_bash_event("rg project .claude/memory")

        response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables={})

        assert response.decision == "block"
        assert response.reason is not None
        assert "Use gobby-memory" in response.reason

    @pytest.mark.asyncio
    async def test_blocks_shell_write_workaround(self, db) -> None:
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
    def test_allows_bare_python(self, db, command: str) -> None:
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
    def test_blocks_unmanaged_package_commands(self, db, command: str) -> None:
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
        ],
    )
    def test_allows_uv_managed_commands(self, db, command: str) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event(command)
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_allows_non_python_command(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("ls -la")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_allows_python_after_chain(self, db) -> None:
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
    def test_blocks_package_management_after_separator(self, db, command: str) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event(command)
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_allows_uv_run_python_after_chain(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("cd /tmp && uv run python test.py")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_blocks_package_command_at_top_level(self, db) -> None:
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

    def test_allows_bare_python_through_normalized_exec_command(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_shell_alias_event("exec_command", "python script.py")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_blocks_package_management_through_normalized_exec_command(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_shell_alias_event("exec_command", "python3.13 -m pip install x")
        assert engine._should_block(_require_uv_effect(), event) is True
