"""Tests for memory-lifecycle rules.

Verifies memory lifecycle rules sync correctly and have proper structure.
Rules that were merged into context-handoff (preserve-context-on-compact)
are tested there instead.

Active memory-lifecycle rules:
- reset-memory-tracking-on-start: set_variable on session_start
- bootstrap-session-title-on-prompt: heuristic title bootstrap on first prompt
- cancel-stale-memory-recall-helpers: mcp_call on turn_start before delivery
- memory-recall-on-prompt: mcp_call on turn_start
- memory-capture-nudge: inject_context on turn_start
- require-memory-review-before-status: block on before_tool (close_task, submit_for_review, approve_review, reject_review)
- clear-memory-review-on-create: set_variable on before_tool

"""

from __future__ import annotations

import json

import pytest

from gobby.adapters.claude_code import ClaudeCodeAdapter
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

MEMORY_RULES = {
    "reset-memory-tracking-on-start",
    "bootstrap-session-title-on-prompt",
    "cancel-stale-memory-recall-helpers",
    "memory-recall-on-prompt",
    "memory-capture-nudge",
    "require-memory-review-before-status",
    "clear-memory-review-on-create",
}


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    return sync_bundled_rules(db, get_bundled_rules_path())


class TestMemoryLifecycleSync:
    """Test that memory-lifecycle rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All memory-lifecycle rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        for rule_name in MEMORY_RULES:
            assert rule_name in rule_names, f"Missing rule: {rule_name}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All memory-lifecycle rules should have group='memory-lifecycle'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in MEMORY_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "memory-lifecycle", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in MEMORY_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                for effect in body.resolved_effects:
                    assert effect.type in {
                        "set_variable",
                        "inject_context",
                        "mcp_call",
                        "block",
                    }


# ═══════════════════════════════════════════════════════════════════════
# reset-memory-tracking-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestResetMemoryTrackingOnStart:
    """Reset injected_memory_ids on context loss (session_start)."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("reset-memory-tracking-on-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "injected_memory_ids"

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("reset-memory-tracking-on-start")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "clear" in body.when
        assert "compact" in body.when


# ═══════════════════════════════════════════════════════════════════════
# bootstrap-session-title-on-prompt
# ═══════════════════════════════════════════════════════════════════════


class TestBootstrapSessionTitleOnPrompt:
    """Bootstrap a heuristic title before the first completed turn."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("bootstrap-session-title-on-prompt")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects[0].type == "mcp_call"
        assert body.effects[0].server == "gobby-memory"
        assert body.effects[0].tool == "bootstrap_session_title"
        assert body.effects[0].background is False

    def test_has_prompt_guard(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("bootstrap-session-title-on-prompt")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "len(" in body.when

    def test_claude_user_prompt_submit_matches_prompt_guard(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("bootstrap-session-title-on-prompt")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None

        event = ClaudeCodeAdapter().translate_to_hook_event(
            {
                "hook_type": "user-prompt-submit",
                "input_data": {
                    "session_id": "ext-claude",
                    "user_prompt": "Fix the Claude tmux title regression",
                },
            }
        )

        evaluator = SafeExpressionEvaluator({"event": {"data": event.data}}, {"len": len})
        assert evaluator.evaluate(body.when) is True

    def test_slash_command_first_prompt_passes_guard(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("bootstrap-session-title-on-prompt")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None

        event = ClaudeCodeAdapter().translate_to_hook_event(
            {
                "hook_type": "user-prompt-submit",
                "input_data": {
                    "session_id": "ext-claude",
                    "user_prompt": "/gobby plan fix the tmux title regression",
                },
            }
        )

        evaluator = SafeExpressionEvaluator({"event": {"data": event.data}}, {"len": len})
        assert evaluator.evaluate(body.when) is True


# ═══════════════════════════════════════════════════════════════════════
# memory-recall-on-prompt
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryRecallOnPrompt:
    """Recall relevant memories before agent prompt."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-recall-on-prompt")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects[0].type == "mcp_call"
        assert body.effects[0].server == "gobby-memory"
        assert body.effects[0].tool == "search_memories"
        assert body.effects[0].arguments["min_score"] == 0.7

    def test_not_background(self, db, manager) -> None:
        """Recall must block to inject context."""
        _sync_bundled(db)
        row = manager.get_by_name("memory-recall-on-prompt")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effects[0].background is False


# ═══════════════════════════════════════════════════════════════════════
# cancel-stale-memory-recall-helpers
# ═══════════════════════════════════════════════════════════════════════


class TestCancelStaleMemoryRecallHelpers:
    """Cancel stale recall helpers before pending-message delivery."""

    def test_event_priority_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("cancel-stale-memory-recall-helpers")
        assert row is not None
        assert row.enabled is True
        assert row.priority == 5

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects[0].type == "mcp_call"
        assert body.effects[0].server == "gobby-agents"
        assert body.effects[0].tool == "cancel_stale_helpers"
        assert body.effects[0].arguments["parent_session_id"] == (
            "{{ event.metadata.get('_platform_session_id') }}"
        )
        assert body.effects[0].arguments["agent_name"] == "memory-recall-helper"
        assert body.effects[0].inject_result is True
        assert body.effects[0].background is False

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("cancel-stale-memory-recall-helpers")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "is_spawned_agent" in body.when
        assert "_platform_session_id" in body.when
        assert "memory_recall_helper_enabled" not in body.when


# ═══════════════════════════════════════════════════════════════════════
# memory-capture-nudge
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryCaptureNudge:
    """Nudge agent to save user preferences."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-capture-nudge")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects[0].type == "inject_context"
        assert body.effects[0].template is not None
        assert "create_memory" in body.effects[0].template

    def test_has_when_condition(self, db, manager) -> None:
        """Only nudge on substantial prompts (not slash commands)."""
        _sync_bundled(db)
        row = manager.get_by_name("memory-capture-nudge")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "prompt" in body.when


# ═══════════════════════════════════════════════════════════════════════
# require-memory-review-before-status
# ═══════════════════════════════════════════════════════════════════════


class TestRequireMemoryReviewBeforeStatus:
    """Gate task status transitions until agent reviews memories."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("require-memory-review-before-status")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "block"
        assert body.effects[0].reason is not None
        assert "create_memory" in body.effects[0].reason

    def test_blocks_all_status_transitions(self, db, manager) -> None:
        """Should block close_task and all review lifecycle transitions."""
        _sync_bundled(db)
        row = manager.get_by_name("require-memory-review-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        mcp_tools = body.effects[0].mcp_tools
        assert "gobby-tasks:close_task" in mcp_tools
        assert "gobby-tasks-ops:submit_for_review" in mcp_tools
        assert "gobby-tasks-ops:approve_review" in mcp_tools
        assert "gobby-tasks-ops:reject_review" in mcp_tools

    def test_has_when_condition(self, db, manager) -> None:
        """Only block when memory_review_completed is not set."""
        _sync_bundled(db)
        row = manager.get_by_name("require-memory-review-before-status")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "memory_review_completed" in body.when
        assert "session_edited_files" in body.when


# ═══════════════════════════════════════════════════════════════════════
# clear-memory-review-on-create
# ═══════════════════════════════════════════════════════════════════════


class TestClearMemoryReviewOnCreate:
    """Set memory_review_completed flag when create_memory is called."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("clear-memory-review-on-create")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "memory_review_completed"
        assert body.effects[0].value is True

    def test_has_when_condition(self, db, manager) -> None:
        """Must match create_memory on gobby-memory server."""
        _sync_bundled(db)
        row = manager.get_by_name("clear-memory-review-on-create")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "create_memory" in body.when
        assert "gobby-memory" in body.when
