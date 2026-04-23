"""Tests for bundled tool-hygiene rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_tool_hygiene.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    return sync_bundled_rules(db, get_bundled_rules_path())


class TestToolHygieneSync:
    """Test that tool-hygiene.yaml syncs correctly."""

    def test_bundled_file_syncs_target_rules(self, db, manager) -> None:
        """Key tool-hygiene rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        assert "block-escaped-quotes" in rule_names
        assert "require-uv" in rule_names
        assert "track-pending-memory-review" in rule_names

    def test_all_rules_have_group(self, db, manager) -> None:
        """All tool-hygiene rules should have group='tool-hygiene'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in {"block-escaped-quotes", "require-uv", "track-pending-memory-review"}:
                body = json.loads(row.definition_json)
                assert body.get("group") == "tool-hygiene", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in {"block-escaped-quotes", "require-uv", "track-pending-memory-review"}:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                effect_types = {e.type for e in body.resolved_effects}
                assert effect_types <= {"block", "set_variable", "rewrite_input", "inject_context"}


class TestBlockEscapedQuotesRule:
    """Verify block-escaped-quotes uses the grouped rule schema."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("block-escaped-quotes")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"

    def test_reason_mentions_escaped_quotes(self, db, manager) -> None:
        _sync_bundled(db)

        row = manager.get_by_name("block-escaped-quotes")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.effects[0].reason is not None
        assert "escaped quotes" in body.effects[0].reason.lower()


REQUIRE_UV_REASON = "Bare python/pip is not permitted in this repo. Use uv instead."
REQUIRE_UV_COMMAND_PATTERN = r"(^|(?<=[;&|]))\s*(?:sudo\s+)?(?:pip3?\b|python(?:3(?:\.\d+)?)?\b)"


class TestRequireUvRule:
    """Verify require-uv blocks naked python/pip commands."""

    def test_uses_single_block_effect(self, db, manager) -> None:
        """require-uv should only block matching Bash commands."""
        _sync_bundled(db)

        row = manager.get_by_name("require-uv")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert row.description == "Block bare python/pip; require uv"
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
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        effect_types = {e.type for e in body.resolved_effects}
        assert "rewrite_input" not in effect_types
        assert "inject_context" not in effect_types

    def test_has_when_condition(self, db, manager) -> None:
        """require-uv should only fire when require_uv variable is set."""
        _sync_bundled(db)

        row = manager.get_by_name("require-uv")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "require_uv" in body.when

    @pytest.mark.asyncio
    async def test_bundled_rule_skips_already_compliant_uv_python(self, db, manager) -> None:
        """Compliant uv commands should not block or emit modified_input."""
        _sync_bundled(db)

        event = _make_bash_event(
            "uv run python -c \"print('hello')\"",
            source=SessionSource.CODEX,
        )
        engine = RuleEngine(db)

        response = await engine.evaluate(event, session_id="sess-1", variables={"require_uv": True})

        assert response.decision == "allow"
        assert response.modified_input is None

    @pytest.mark.asyncio
    async def test_bundled_rule_blocks_bare_python_without_modified_input(
        self, db, manager
    ) -> None:
        """Bare python should block directly without rewrite retry payloads."""
        _sync_bundled(db)

        event = _make_bash_event("python script.py", source=SessionSource.CODEX)
        engine = RuleEngine(db)

        response = await engine.evaluate(event, session_id="sess-1", variables={"require_uv": True})

        assert response.decision == "block"
        assert response.reason == f"Rule enforced by Gobby: [require-uv]\n{REQUIRE_UV_REASON}"
        assert response.modified_input is None
        assert response.auto_approve is False


class TestTrackPendingMemoryReview:
    """Verify track-pending-memory-review sets variable after edits."""

    def test_is_set_variable_effect(self, db, manager) -> None:
        """track-pending-memory-review should use set_variable effect."""
        _sync_bundled(db)

        row = manager.get_by_name("track-pending-memory-review")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.effects[0].type == "set_variable"

    def test_sets_memory_review_completed_variable(self, db, manager) -> None:
        """Should set the memory_review_completed variable to false."""
        _sync_bundled(db)

        row = manager.get_by_name("track-pending-memory-review")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.effects[0].variable == "memory_review_completed"
        assert body.effects[0].value is False

    def test_has_when_condition_for_canonical_mutation(self, db, manager) -> None:
        """Should fire for any canonical file mutation."""
        _sync_bundled(db)

        row = manager.get_by_name("track-pending-memory-review")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "canonical_repo_mutation" in body.when


def _make_bash_event(command: str, source: SessionSource = SessionSource.CLAUDE) -> HookEvent:
    """Create a before_tool HookEvent with command nested in tool_input (like real adapters)."""
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
        source=source,
        timestamp=datetime.now(UTC),
        data={"tool_name": "Bash", "tool_input": {"command": command}},
    )


def _make_shell_alias_event(tool_name: str, command: str) -> HookEvent:
    """Create a before_tool HookEvent for shell aliases."""
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="test-session",
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

    def test_blocks_naked_python(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("python script.py")
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_blocks_naked_pip(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("pip install requests")
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_blocks_python3_inline(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("python3 -c \"print('hi')\"")
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_allows_uv_run_python(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("uv run python -c \"print('hello')\"")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_allows_uv_run_pytest(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("uv run pytest tests/ -v")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_allows_uv_pip_install(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("uv pip install requests")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_allows_non_python_command(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("ls -la")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_blocks_python_after_chain(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("cd /tmp && python test.py")
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_blocks_pip_after_chain(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("cd /tmp && pip install x")
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_allows_uv_run_python_after_chain(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_bash_event("cd /tmp && uv run python test.py")
        assert engine._should_block(_require_uv_effect(), event) is False

    def test_blocks_when_command_at_top_level(self, db) -> None:
        """Legacy path: command at top level of event.data still works."""
        engine = RuleEngine(db)
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Bash", "command": "python script.py"},
        )
        assert engine._should_block(_require_uv_effect(), event) is True

    def test_blocks_shell_aliases_with_bash_rule(self, db) -> None:
        engine = RuleEngine(db)
        event = _make_shell_alias_event("exec_command", "python script.py")
        assert engine._should_block(_require_uv_effect(), event) is True
