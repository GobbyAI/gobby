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
- load-memory-guidance-on-initial-turn: load_skill on the initial turn_start
- check-memory-guidance-on-initial-stop: acknowledged block on the first turn_end
- remind-memory-guidance-on-later-turns: inject_context on later parent turn_starts
- queue-task-memory-review-after-close: set_variable on after_tool close_task
- review-closed-task-memories-on-stop: acknowledged block on turn_end
- guard-plan-memory-writes: one-time block on create_memory and update_memory
- require-memory-recall-before-tool: block on before_tool
- require-memory-recall-before-turn-end: block on turn_end
- clear-memory-review-on-create: set_variable on before_tool

"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.skills.formatting import skill_fetch_directive
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
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
    "load-memory-guidance-on-initial-turn",
    "check-memory-guidance-on-initial-stop",
    "remind-memory-guidance-on-later-turns",
    "queue-task-memory-review-after-close",
    "review-closed-task-memories-on-stop",
    "guard-plan-memory-writes",
    "require-memory-recall-before-tool",
    "require-memory-recall-before-turn-end",
}

REMOVED_HELPER_RULES = {
    "bootstrap-session-title-on-prompt",
    "cancel-stale-memory-recall-helpers",
    "memory-capture-nudge",
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
                        "load_skill",
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
    """Drain undigested backlog in bounded batches at each turn_start."""

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
        assert effect.arguments == {"catch_up": True}
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
# layered memory guidance and post-close review
# ═══════════════════════════════════════════════════════════════════════

SESSION_ID = "11111111-1111-4111-8111-111111111111"
TASK_ID = "22222222-2222-4222-8222-222222220043"


def _turn_end_event(
    event_type: HookEventType = HookEventType.STOP,
    *,
    source: SessionSource = SessionSource.CODEX,
    data: dict[str, Any] | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=SESSION_ID,
        source=source,
        timestamp=datetime.now(UTC),
        data=data or {},
    )


def _initial_gate_variables(**overrides: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "_memory_initial_stop_checked": False,
        "loaded_skills": [],
        "open_tool_errors": [],
    }
    variables.update(overrides)
    return variables


def _pending_review(task_ref: str, summary: str) -> dict[str, str]:
    number = task_ref.lstrip("#")
    return {
        "closure_id": f"task-{number}:closed",
        "task_id": f"task-{number}",
        "task_ref": task_ref,
        "changes_summary": summary,
    }


# Hardcoded turn-end overrides in RuleEngine core: each returns before the
# rule-level block is delivered, so gate state must survive it untouched.
TURN_END_OVERRIDES = [
    pytest.param({"tool_block_pending": True}, "block", "tool-failure-recovery", id="tool_block"),
    pytest.param({"edit_write_pending": True}, "block", "edit-write-recovery", id="edit_write"),
    pytest.param({"force_allow_stop": True}, "allow", "", id="force_allow"),
]


def _review_variables(*pending: dict[str, str], **overrides: Any) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "_memory_initial_stop_checked": True,
        "loaded_skills": ["memory"],
        "_memory_pending_task_reviews": list(pending),
        "_memory_review_stop_delivered": False,
    }
    variables.update(overrides)
    return variables


class TestLayeredMemoryGuidance:
    def test_initial_turn_requests_memory_skill(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("load-memory-guidance-on-initial-turn")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects

        assert body.event.value == "turn_start"
        assert effects[0].type == "load_skill"
        assert effects[0].skill == "memory"
        assert "_memory_initial_stop_checked" in (body.when or "")
        assert "has_open_tool_error" in (body.when or "")

    @pytest.mark.asyncio
    async def test_initial_turn_emits_skill_directive_and_records_reload(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        variables: dict[str, Any] = {
            "_memory_initial_stop_checked": False,
            "is_spawned_agent": False,
            "parent_turn_seq": 0,
            "loaded_skills": [],
            "workflow_requested_skills": [],
            "open_tool_errors": [],
        }
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="11111111-1111-4111-8111-111111111111",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"prompt": "Implement the requested memory guidance."},
        )

        response = await RuleEngine(db).evaluate(event, event.session_id, variables)

        assert skill_fetch_directive("memory") in (response.context or "")
        assert "memory" in variables["workflow_requested_skills"]

    def test_initial_turn_end_gate_sets_flag_only_when_passed_or_acknowledged(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("check-memory-guidance-on-initial-stop")
        assert row is not None
        assert row.priority == 1
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects

        assert body.event.value == "turn_end"
        assert effects[0].type == "set_variable"
        assert effects[0].variable == "_memory_initial_stop_checked"
        assert effects[0].value is True
        assert "skill_loaded('memory')" in (effects[0].when or "")
        assert "has_open_tool_error" in (effects[0].when or "")
        assert effects[1].type == "block"
        assert effects[1].acknowledge_variable == "_memory_initial_stop_checked"
        assert "not skill_loaded('memory')" in (effects[1].when or "")
        assert "has_open_tool_error" in (effects[1].when or "")

    def test_matching_fetch_failure_fails_initial_stop_open(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("check-memory-guidance-on-initial-stop")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        block_when = body.resolved_effects[1].when
        assert block_when is not None

        evaluator = SafeExpressionEvaluator(
            {"variables": {"loaded_skills": []}},
            {
                "skill_loaded": lambda _name: False,
                "has_open_tool_error": lambda _tool, _arguments: True,
            },
        )
        assert evaluator.evaluate(block_when) is False

    def test_later_reminder_is_parent_only_and_deduplicated_per_turn(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("remind-memory-guidance-on-later-turns")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        assert body.resolved_effects[0].template == (
            "Memory reminder: use recalled memories; search `gobby-memory` when injected "
            "context is insufficient. Record only explicitly requested or durable, "
            "non-obvious knowledge. Most turns need no memory write.\n"
        )

        first = SafeExpressionEvaluator(
            {
                "variables": {
                    "is_spawned_agent": False,
                    "_memory_initial_stop_checked": True,
                    "parent_turn_seq": 8,
                }
            },
            {},
        )
        duplicate = SafeExpressionEvaluator(
            {
                "variables": {
                    "is_spawned_agent": False,
                    "_memory_initial_stop_checked": True,
                    "parent_turn_seq": 8,
                    "_memory_reminder_turn_seq": 8,
                }
            },
            {},
        )
        spawned = SafeExpressionEvaluator(
            {
                "variables": {
                    "is_spawned_agent": True,
                    "_memory_initial_stop_checked": True,
                    "parent_turn_seq": 8,
                }
            },
            {},
        )

        assert body.when is not None
        assert first.evaluate(body.when) is True
        assert duplicate.evaluate(body.when) is False
        assert spawned.evaluate(body.when) is False

    @pytest.mark.asyncio
    async def test_first_turn_end_blocks_once_and_consumes_gate(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        variables = _initial_gate_variables()
        engine = RuleEngine(db)
        event = _turn_end_event()

        first = await engine.evaluate(event, SESSION_ID, variables)
        second = await engine.evaluate(event, SESSION_ID, variables)

        assert first.decision == "block"
        assert skill_fetch_directive("memory") in (first.reason or "")
        assert variables["_memory_initial_stop_checked"] is True
        assert "check-memory-guidance-on-initial-stop" not in (second.reason or "")

    @pytest.mark.asyncio
    async def test_loaded_skill_passes_initial_gate_without_block(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        variables = _initial_gate_variables(loaded_skills=["memory"])

        response = await RuleEngine(db).evaluate(_turn_end_event(), SESSION_ID, variables)

        assert "check-memory-guidance-on-initial-stop" not in (response.reason or "")
        assert variables["_memory_initial_stop_checked"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("override", "decision", "marker"), TURN_END_OVERRIDES)
    async def test_turn_end_override_leaves_initial_gate_armed(
        self,
        db: HubDatabase,
        override: dict[str, bool],
        decision: str,
        marker: str,
    ) -> None:
        _sync_bundled(db)
        variables = _initial_gate_variables(**override)
        engine = RuleEngine(db)
        event = _turn_end_event()

        overridden = await engine.evaluate(event, SESSION_ID, variables)
        overridden_flag = variables["_memory_initial_stop_checked"]
        for key in override:
            variables[key] = False
        delivered = await engine.evaluate(event, SESSION_ID, variables)

        assert overridden.decision == decision
        assert marker in (overridden.reason or "")
        assert skill_fetch_directive("memory") not in (overridden.reason or "")
        assert overridden_flag is False
        assert delivered.decision == "block"
        assert skill_fetch_directive("memory") in (delivered.reason or "")
        assert variables["_memory_initial_stop_checked"] is True

    @pytest.mark.asyncio
    async def test_manual_compact_stop_skips_initial_gate(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        variables = _initial_gate_variables()
        engine = RuleEngine(db)

        await engine.evaluate(
            _turn_end_event(HookEventType.PRE_COMPACT, data={"trigger": "manual"}),
            SESSION_ID,
            variables,
        )
        response = await engine.evaluate(_turn_end_event(), SESSION_ID, variables)

        assert response.decision == "allow"
        assert variables["_memory_initial_stop_checked"] is False

    @pytest.mark.asyncio
    async def test_after_agent_fires_initial_gate_for_other_providers(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        variables = _initial_gate_variables()
        event = _turn_end_event(HookEventType.AFTER_AGENT, source=SessionSource.QWEN)

        response = await RuleEngine(db).evaluate(event, SESSION_ID, variables)

        assert response.decision == "block"
        assert skill_fetch_directive("memory") in (response.reason or "")
        assert variables["_memory_initial_stop_checked"] is True


class TestPostCloseMemoryReviewRules:
    def test_queue_rule_uses_normalized_success_and_resets_delivery_flag(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("queue-task-memory-review-after-close")
        assert row is not None
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects

        assert body.event.value == "after_tool"
        assert "mcp_server" in (body.when or "")
        assert "mcp_tool" in (body.when or "")
        assert "tool_call_succeeded" in (body.when or "")
        assert effects[0].variable == "_memory_pending_task_reviews"
        assert effects[0].value == "queue_memory_review_close(event.data, tool_input)"
        assert effects[1].type == "set_variable"
        assert effects[1].variable == "_memory_review_stop_delivered"
        assert effects[1].value is False

    def test_turn_end_review_is_single_acknowledged_block(
        self, db: HubDatabase, manager: RuleDefinitionManager
    ) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("review-closed-task-memories-on-stop")
        assert row is not None
        assert row.priority == 2
        body = RuleDefinitionBody.model_validate(row.definition_json)
        effects = body.resolved_effects

        assert body.event.value == "turn_end"
        assert "_memory_pending_task_reviews" in (body.when or "")
        assert "_memory_review_stop_delivered" in (body.when or "")
        assert len(effects) == 1
        assert effects[0].type == "block"
        assert effects[0].acknowledge_variable == "_memory_review_stop_delivered"
        reason = effects[0].reason or ""
        assert "{% for item in variables.get('_memory_pending_task_reviews') or [] %}" in reason
        assert "review_task_memories" in reason
        assert "source_task_id" in reason

    @pytest.mark.asyncio
    async def test_turn_end_renders_pending_closures_and_delivers_once(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        pending = [
            _pending_review("#42", "Implemented layered memory guidance."),
            _pending_review("#43", "Documented the review tool."),
        ]
        variables = _review_variables(*pending)
        engine = RuleEngine(db)
        event = _turn_end_event()

        first = await engine.evaluate(event, SESSION_ID, variables)
        second = await engine.evaluate(event, SESSION_ID, variables)

        assert first.decision == "block"
        reason = first.reason or ""
        assert "#42 (task_id `task-42`): Implemented layered memory guidance." in reason
        assert "#43 (task_id `task-43`): Documented the review tool." in reason
        assert "gobby-memory:review_task_memories(task_id, changes_summary)" in reason
        assert variables["_memory_review_stop_delivered"] is True
        assert variables["_memory_pending_task_reviews"] == pending
        assert "review-closed-task-memories-on-stop" not in (second.reason or "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("override", "decision", "marker"), TURN_END_OVERRIDES)
    async def test_turn_end_override_preserves_pending_review(
        self,
        db: HubDatabase,
        override: dict[str, bool],
        decision: str,
        marker: str,
    ) -> None:
        _sync_bundled(db)
        pending = _pending_review("#42", "Completed work.")
        variables = _review_variables(pending, **override)
        engine = RuleEngine(db)
        event = _turn_end_event()

        overridden = await engine.evaluate(event, SESSION_ID, variables)
        overridden_flag = variables["_memory_review_stop_delivered"]
        for key in override:
            variables[key] = False
        delivered = await engine.evaluate(event, SESSION_ID, variables)
        settled = await engine.evaluate(event, SESSION_ID, variables)

        assert overridden.decision == decision
        assert marker in (overridden.reason or "")
        assert "review-closed-task-memories-on-stop" not in (overridden.reason or "")
        assert overridden_flag is False
        assert delivered.decision == "block"
        assert "#42 (task_id `task-42`): Completed work." in (delivered.reason or "")
        assert variables["_memory_pending_task_reviews"] == [pending]
        assert variables["_memory_review_stop_delivered"] is True
        assert "review-closed-task-memories-on-stop" not in (settled.reason or "")

    @pytest.mark.asyncio
    async def test_manual_compact_stop_preserves_pending_review(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        pending = _pending_review("#42", "Completed work.")
        variables = _review_variables(pending)
        engine = RuleEngine(db)

        await engine.evaluate(
            _turn_end_event(HookEventType.PRE_COMPACT, data={"trigger": "manual"}),
            SESSION_ID,
            variables,
        )
        response = await engine.evaluate(_turn_end_event(), SESSION_ID, variables)

        assert response.decision == "allow"
        assert variables["_memory_pending_task_reviews"] == [pending]
        assert variables["_memory_review_stop_delivered"] is False

    @pytest.mark.asyncio
    async def test_after_agent_aggregates_both_memory_gates_once(self, db: HubDatabase) -> None:
        _sync_bundled(db)
        variables = _review_variables(
            _pending_review("#42", "Completed work."),
            _memory_initial_stop_checked=False,
            loaded_skills=[],
            open_tool_errors=[],
        )
        engine = RuleEngine(db)
        event = _turn_end_event(HookEventType.AFTER_AGENT, source=SessionSource.QWEN)

        first = await engine.evaluate(event, SESSION_ID, variables)
        second = await engine.evaluate(event, SESSION_ID, variables)

        assert first.decision == "block"
        reason = first.reason or ""
        assert "aggregated:2-gates" in reason
        assert "[check-memory-guidance-on-initial-stop]" in reason
        assert "[review-closed-task-memories-on-stop]" in reason
        assert "#42 (task_id `task-42`): Completed work." in reason
        assert variables["_memory_initial_stop_checked"] is True
        assert variables["_memory_review_stop_delivered"] is True
        assert "check-memory-guidance-on-initial-stop" not in (second.reason or "")
        assert "review-closed-task-memories-on-stop" not in (second.reason or "")

    @pytest.mark.asyncio
    async def test_later_close_requeues_only_new_closure_and_resets_flag(
        self, db: HubDatabase
    ) -> None:
        _sync_bundled(db)
        task_manager = MagicMock()
        task_manager.get_task.return_value = SimpleNamespace(
            id=TASK_ID,
            seq_num=43,
            task_type="task",
            category="code",
            closed_reason="completed",
            closed_at=datetime(2026, 8, 25, tzinfo=UTC),
            commits=["abc1234"],
        )
        task_manager.list_tasks.return_value = []
        delivered = _pending_review("#42", "Earlier closure.")
        variables = _review_variables(delivered, _memory_review_stop_delivered=True)
        engine = RuleEngine(db, task_manager=task_manager)
        close_event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=SESSION_ID,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {
                        "task_id": "#43",
                        "changes_summary": "Second closure.",
                        "commit_sha": "abc1234",
                    },
                },
                "tool_output": {
                    "success": True,
                    "closed": True,
                    "task_id": TASK_ID,
                    "commit_shas": ["abc1234"],
                },
            },
        )

        await engine.evaluate(close_event, SESSION_ID, variables)
        queued = list(variables["_memory_pending_task_reviews"])
        queued_flag = variables["_memory_review_stop_delivered"]
        response = await engine.evaluate(_turn_end_event(), SESSION_ID, variables)

        assert queued == [
            {
                "closure_id": f"{TASK_ID}:2026-08-25T00:00:00+00:00",
                "task_id": TASK_ID,
                "task_ref": "#43",
                "changes_summary": "Second closure.",
            }
        ]
        assert queued_flag is False
        assert f"#43 (task_id `{TASK_ID}`): Second closure." in (response.reason or "")
        assert "Earlier closure." not in (response.reason or "")
        assert variables["_memory_review_stop_delivered"] is True


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
