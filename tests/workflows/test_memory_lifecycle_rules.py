"""Tests for memory-lifecycle rules.

Verifies memory lifecycle rules sync correctly and have proper structure.
Rules that were merged into context-handoff (preserve-context-on-compact)
are tested there instead.

Active memory-lifecycle rules:
- digest-on-plan-turn-end: mcp_call on provider-specific plan boundaries
- digest-catch-up-on-turn-start: mcp_call on turn_start to catch up undigested prior turns
- reset-memory-tracking-on-start: set_variable on session_start
- increment-parent-turn-seq: set_variable on turn_start before daemon recall
- memory-recall-on-prompt: mcp_call on turn_start
- memory-capture-nudge: inject_context on turn_start
- guard-plan-memory-writes: one-time block on create_memory and update_memory
- require-memory-recall-before-tool: block on before_tool
- require-memory-recall-before-turn-end: block on turn_end
- clear-memory-review-on-create: set_variable on before_tool

"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks.events import HookEventType
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

MEMORY_RULES = {
    "digest-on-response",
    "digest-catch-up-on-turn-start",
    "digest-on-plan-turn-end",
    "reset-memory-tracking-on-start",
    "increment-parent-turn-seq",
    "memory-recall-on-prompt",
    "memory-capture-nudge",
    "guard-plan-memory-writes",
    "require-memory-recall-before-tool",
    "require-memory-recall-before-turn-end",
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
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


def _sync_bundled(db: HubDatabase) -> dict[str, Any]:
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync_rules import get_bundled_rules_path

    return sync_bundled_rules(db, get_bundled_rules_path())


class TestMemoryLifecycleSync:
    """Test that memory-lifecycle rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All memory-lifecycle rules should sync to rule_definitions."""
        _sync_bundled(db)

        rules = manager.list_all()
        rule_names = {r.name for r in rules}

        for rule_name in MEMORY_RULES:
            assert rule_name in rule_names, f"Missing rule: {rule_name}"
        for removed_name in REMOVED_HELPER_RULES:
            assert removed_name not in rule_names, f"Removed helper rule synced: {removed_name}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All memory-lifecycle rules should have group='memory-lifecycle'."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in MEMORY_RULES:
                body = row.definition_json
                assert body.get("group") == "memory-lifecycle", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all()
        for row in rules:
            if row.name in MEMORY_RULES:
                body = RuleDefinitionBody.model_validate(row.definition_json)
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

    def test_event_and_effect(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("digest-on-plan-turn-end")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
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
            ("ExitSpecMode", True),
            ("AskUser", True),
            ("Bash", False),
        ],
    )
    def test_matches_plan_boundaries(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
        tool_name: str,
        matches: bool,
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("digest-on-plan-turn-end")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
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
# digest-catch-up-on-turn-start
# ═══════════════════════════════════════════════════════════════════════


class TestDigestCatchUpOnTurnStart:
    """Catch up undigested prior turns at the next turn_start (e.g. after daemon restart)."""

    def test_event_and_effect(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("digest-catch-up-on-turn-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.effects is not None
        effect = body.effects[0]

        assert body.event.value == "turn_start"
        assert body.when is None
        assert effect.type == "mcp_call"
        assert effect.server == "gobby-memory"
        assert effect.tool == "build_turn_and_digest"
        assert effect.arguments == {"prior_turn_only": True}
        assert effect.background is True


# ═══════════════════════════════════════════════════════════════════════
# reset-memory-tracking-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestResetMemoryTrackingOnStart:
    """Reset injected_memory_ids on context loss (session_start)."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("reset-memory-tracking-on-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "injected_memory_ids"

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("reset-memory-tracking-on-start")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "clear" in body.when
        assert "compact" in body.when


# memory-recall-on-prompt
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryRecallOnPrompt:
    """Substantive recall runs synchronously once per parent turn."""

    def test_inline_rule_uses_single_recall_tool_and_attempt_watermark(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-recall-on-prompt")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.when is not None
        assert "is_spawned_agent" in body.when
        assert "memory_recall_attempted_turn_seq" in body.when
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "memory_recall_attempted_turn_seq"
        recall = body.effects[1]
        assert recall.type == "mcp_call"
        assert recall.server == "gobby-memory"
        assert recall.tool == "recall_memories_for_prompt"
        assert recall.background is False
        assert recall.inject_result is True
        assert all(effect.tool != "search_memories" for effect in body.effects)

    def test_duplicate_hook_for_same_parent_turn_is_rejected(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-recall-on-prompt")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None

        first = SafeExpressionEvaluator(
            {
                "variables": {
                    "is_spawned_agent": False,
                    "parent_turn_seq": 7,
                }
            },
            {},
        )
        duplicate = SafeExpressionEvaluator(
            {
                "variables": {
                    "is_spawned_agent": False,
                    "parent_turn_seq": 7,
                    "memory_recall_attempted_turn_seq": 7,
                }
            },
            {},
        )

        assert first.evaluate(body.when) is True
        assert duplicate.evaluate(body.when) is False


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

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "parent_turn_seq"
        assert body.effects[0].value == "{{ (variables.parent_turn_seq | int) + 1 }}"

    def test_has_fail_closed_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("increment-parent-turn-seq")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
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
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects[0].type == "inject_context"
        assert body.effects[0].template is not None
        assert "create_memory" in body.effects[0].template
        assert "Draft direction, enhancement suggestions, and review findings" in (
            body.effects[0].template
        )
        assert "plan artifact or evidence" in body.effects[0].template

    def test_has_when_condition(self, db, manager) -> None:
        """Only nudge on substantial prompts (not slash commands)."""
        _sync_bundled(db)
        row = manager.get_by_name("memory-capture-nudge")
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "prompt" in body.when


# ═══════════════════════════════════════════════════════════════════════
# clear-memory-review-on-create
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
# guard-plan-memory-writes
# ═══════════════════════════════════════════════════════════════════════


class TestGuardPlanMemoryWrites:
    """Guard planning-context memory writes once per planning epoch."""

    def test_rule_contract(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("guard-plan-memory-writes")
        assert row is not None
        assert row.enabled is True
        assert row.priority == 11

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.agent_scope is None
        assert body.effects is not None
        effect = body.effects[0]
        assert effect.type == "block"
        assert effect.mcp_tools == [
            "gobby-memory:create_memory",
            "gobby-memory:update_memory",
        ]
        assert effect.acknowledge_variable == "plan_memory_write_nudge_fired"
        assert effect.reason == (
            "Use the plan artifact or evidence for plan drafts, enhancement "
            "suggestions, and review findings. Memory is reserved for explicit "
            "durable user preferences and finalized decisions. Retry only when the "
            "write satisfies that boundary."
        )

    def test_condition_covers_planning_contexts_and_recall_precedence(
        self,
        db: HubDatabase,
        manager: RuleDefinitionManager,
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("guard-plan-memory-writes")
        assert row is not None

        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.when is not None
        assert "variables.get('plan_mode')" in body.when
        assert "not pending_memory_recall_request_id()" in body.when
        for agent_type in (
            "planner",
            "plan-adversary",
            "plan-adversary-taskless",
            "plan-enhancer",
            "plan-enhancer-taskless",
        ):
            assert f"'{agent_type}'" in body.when
