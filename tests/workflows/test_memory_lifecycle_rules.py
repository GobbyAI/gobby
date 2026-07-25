"""Tests for memory-lifecycle rules.

Verifies memory lifecycle rules sync correctly and have proper structure.
Rules that were merged into context-handoff (preserve-context-on-compact)
are tested there instead.

Active memory-lifecycle rules:
- digest-on-plan-turn-end: mcp_call on provider-specific plan boundaries
- reset-memory-tracking-on-start: set_variable on session_start
- increment-parent-turn-seq: set_variable on turn_start before daemon recall
- memory-recall-on-prompt: mcp_call on turn_start
- memory-capture-nudge: inject_context on turn_start
- require-memory-recall-before-tool: block on before_tool
- require-memory-recall-before-turn-end: block on turn_end
- require-memory-review-before-status: block on before_tool (close_task, submit_for_review, approve_review, reject_review)
- clear-memory-review-on-create: set_variable on before_tool

"""

from __future__ import annotations

import json

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks.events import HookEventType
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

MEMORY_RULES = {
    "digest-on-plan-turn-end",
    "reset-memory-tracking-on-start",
    "increment-parent-turn-seq",
    "memory-recall-on-prompt",
    "memory-capture-nudge",
    "require-memory-recall-before-tool",
    "require-memory-recall-before-turn-end",
    "require-memory-review-before-status",
    "clear-memory-review-on-create",
}

REMOVED_HELPER_RULES = {
    "bootstrap-session-title-on-prompt",
    "cancel-stale-memory-recall-helpers",
    "spawn-memory-recall-helper",
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
        for removed_name in REMOVED_HELPER_RULES:
            assert removed_name not in rule_names, f"Removed helper rule synced: {removed_name}"

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

    def test_removed_bootstrap_title_rule_is_orphan_pruned(self, db, manager) -> None:
        obsolete = manager.create(
            name="bootstrap-session-title-on-prompt",
            definition_json=json.dumps(
                {
                    "event": "turn_start",
                    "effects": [{"type": "set_variable", "variable": "obsolete", "value": True}],
                }
            ),
            workflow_type="rule",
            source="installed",
            tags=["gobby"],
        )

        result = _sync_bundled(db)

        assert result["orphaned"] >= 1
        deleted = manager.get(obsolete.id, include_deleted=True)
        assert deleted.deleted_at is not None


# ═══════════════════════════════════════════════════════════════════════
# digest-on-plan-turn-end
# ═══════════════════════════════════════════════════════════════════════


class TestDigestOnPlanTurnEnd:
    """Build a digest at each provider-specific plan turn boundary."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("digest-on-plan-turn-end")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.effects[0].type == "mcp_call"
        assert body.effects[0].server == "gobby-memory"
        assert body.effects[0].tool == "build_turn_and_digest"
        assert body.effects[0].background is True

    @pytest.mark.parametrize(
        ("tool_name", "matches"),
        [
            ("ExitPlanMode", True),
            ("AskUserQuestion", True),
            ("request_user_input", True),
            ("Bash", False),
        ],
    )
    def test_matches_plan_boundaries(self, db, manager, tool_name: str, matches: bool) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("digest-on-plan-turn-end")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None

        native_event = {
            "hook_type": "PostToolUse",
            "input_data": {
                "session_id": "codex-session-123",
                "cwd": "/project",
                "tool_name": tool_name,
            },
            "source": "codex",
        }
        event = CodexHooksAdapter().translate_to_hook_event(native_event)

        assert event is not None
        assert event.event_type == HookEventType.AFTER_TOOL
        assert event.data["tool_name"] == tool_name
        assert SafeExpressionEvaluator({"event": event}, {}).evaluate(body.when) is matches


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


# memory-recall-on-prompt
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryRecallOnPrompt:
    """Legacy raw recall rule is neutralized; daemon-owned recall handles prompts."""

    def test_neutralized_and_inert(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-recall-on-prompt")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.when == "false"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "legacy_memory_recall_rule_disabled"
        assert body.effects[0].value is True
        assert all(effect.tool != "search_memories" for effect in body.effects)


# ═══════════════════════════════════════════════════════════════════════
# increment-parent-turn-seq
# ═══════════════════════════════════════════════════════════════════════


class TestIncrementParentTurnSeq:
    """Increment the parent turn counter before daemon recall."""

    def test_event_priority_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("increment-parent-turn-seq")
        assert row is not None
        assert row.enabled is True
        assert row.priority == 1

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "parent_turn_seq"
        assert body.effects[0].value == "{{ (variables.parent_turn_seq | int) + 1 }}"

    def test_has_fail_closed_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("increment-parent-turn-seq")
        assert row is not None

        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "is_spawned_agent" in body.when
        assert "variables.get('parent_turn_seq') is not none" in body.when
        assert "memory_recall_helper_enabled" not in body.when
        assert "default(0)" not in body.effects[0].value


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
        assert "target_task_has_edits" in body.when
        assert "preview" in body.when
        assert "session_edited_files" not in body.when


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
