"""Tests for context-handoff rules.

Verifies context handoff rules sync correctly and have proper structure:
- clear-pending-context-reset-on-start: set_variable on session_start
- capture-baseline-dirty-files-on-start: mcp_call on session_start
- inject-previous-session-summary: inject_context on session_start
- inject-compact-handoff: inject_context on session_start
- inject-task-context-on-start: inject_context on session_start
- preserve-context-on-compact: set_variable on pre_compact (reset tracking vars)
- auto-compact-after-task-close: after_tool close_task compaction handoff

"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.sync_rules import sync_bundled_rules

pytestmark = pytest.mark.unit

CONTEXT_HANDOFF_RULES = {
    "clear-pending-context-reset-on-start",
    "capture-baseline-dirty-files-on-start",
    "inject-previous-session-summary",
    "inject-compact-handoff",
    "inject-task-context-on-start",
    "prepare-clear-handoff",
    "preserve-context-on-compact",
    "auto-compact-after-task-close",
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

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them
    db.execute("UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'")
    return result


class TestContextHandoffSync:
    """Test that context-handoff rules sync correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All context-handoff rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        for rule_name in CONTEXT_HANDOFF_RULES:
            assert rule_name in rule_names, f"Missing rule: {rule_name}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All context-handoff rules should have group='context-handoff'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in CONTEXT_HANDOFF_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "context-handoff", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in CONTEXT_HANDOFF_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                for effect in body.resolved_effects:
                    assert effect.type in {
                        "set_variable",
                        "inject_context",
                        "mcp_call",
                    }


# ═══════════════════════════════════════════════════════════════════════
# clear-pending-context-reset-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestClearPendingContextResetOnStart:
    """Clear pending_context_reset flag on session_start."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("clear-pending-context-reset-on-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "pending_context_reset"

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("clear-pending-context-reset-on-start")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "pending_context_reset" in body.when


# ═══════════════════════════════════════════════════════════════════════
# capture-baseline-dirty-files-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestCaptureBaselineDirtyFilesOnStart:
    """Capture baseline dirty files on session_start."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("capture-baseline-dirty-files-on-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "mcp_call"
        assert body.effects[0].server == "gobby-sessions"
        assert body.effects[0].tool == "capture_baseline_dirty_files"


# ═══════════════════════════════════════════════════════════════════════
# inject-previous-session-summary
# ═══════════════════════════════════════════════════════════════════════


class TestInjectPreviousSessionSummary:
    """Inject previous session summary on clear."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-previous-session-summary")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "inject_context"
        assert body.effects[0].template is not None
        assert "Previous Session Context" in body.effects[0].template

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-previous-session-summary")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "clear" in body.when


# ═══════════════════════════════════════════════════════════════════════
# inject-compact-handoff
# ═══════════════════════════════════════════════════════════════════════


class TestInjectCompactHandoff:
    """Inject compact handoff context after compaction."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-compact-handoff")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "inject_context"
        assert body.effects[0].template is not None
        assert "Continuation Context" in body.effects[0].template

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-compact-handoff")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "compact" in body.when


# ═══════════════════════════════════════════════════════════════════════
# inject-task-context-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestInjectTaskContextOnStart:
    """Inject active task context on session_start (not resume)."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-task-context-on-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effects[0].type == "inject_context"
        assert body.effects[0].template is not None

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-task-context-on-start")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "resume" in body.when


# ═══════════════════════════════════════════════════════════════════════
# prepare-clear-handoff
# ═══════════════════════════════════════════════════════════════════════


class TestPrepareClearHandoff:
    """Prepare clear/exit handoff on turn_start."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("prepare-clear-handoff")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "turn_start"
        assert body.effects[0].type == "set_variable"
        assert body.effects[0].variable == "handoff_source"
        assert body.effects[0].value == "clear"

    def test_has_clear_and_exit_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("prepare-clear-handoff")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "/clear" in body.when
        assert "/exit" in body.when


# ═══════════════════════════════════════════════════════════════════════
# preserve-context-on-compact (multi-effect)
# ═══════════════════════════════════════════════════════════════════════


class TestPreserveContextOnCompact:
    """Reset tracking variables before compaction."""

    def test_event_is_pre_compact(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "pre_compact"

    def test_has_three_effects(self, db, manager) -> None:
        """Should have 3 set_variable effects."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        assert len(effects) == 3
        assert all(e.type == "set_variable" for e in effects)

    def test_has_gemini_filter(self, db, manager) -> None:
        """Should filter out automatic gemini compactions."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "gemini" in body.when

    def test_resets_injected_memory_ids(self, db, manager) -> None:
        """Should reset injected_memory_ids to empty list."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        memory_reset = [
            e for e in effects if e.type == "set_variable" and e.variable == "injected_memory_ids"
        ]
        assert len(memory_reset) == 1
        assert memory_reset[0].value == []

    def test_sets_pending_context_reset(self, db, manager) -> None:
        """Should set pending_context_reset to true."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        reset_flag = [
            e for e in effects if e.type == "set_variable" and e.variable == "pending_context_reset"
        ]
        assert len(reset_flag) == 1
        assert reset_flag[0].value is True


# ═══════════════════════════════════════════════════════════════════════
# auto-compact-after-task-close
# ═══════════════════════════════════════════════════════════════════════


class TestAutoCompactAfterTaskClose:
    """Compact web chat or nudge terminal after closing one task with substantial work left."""

    def test_event_and_effects(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("auto-compact-after-task-close")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.event.value == "after_tool"
        effects = body.resolved_effects
        assert len(effects) == 3

        compact_calls = [effect for effect in effects if effect.type == "mcp_call"]
        dedupe_sets = [
            effect
            for effect in effects
            if effect.type == "set_variable"
            and effect.variable == "_auto_compact_after_task_close_queued_for"
        ]
        fallback_nudges = [effect for effect in effects if effect.type == "inject_context"]
        assert len(compact_calls) == 1
        assert len(dedupe_sets) == 1
        assert len(fallback_nudges) == 1

        compact_call = compact_calls[0]
        assert compact_call.server == "gobby-sessions"
        assert compact_call.tool == "compact_self"
        assert compact_call.background is True
        assert compact_call.when is None
        assert compact_call.arguments == {"rule_name": "auto-compact-after-task-close"}

        fallback_nudge = fallback_nudges[0]
        assert "compact_call_queue_failed" in (fallback_nudge.when or "")
        assert fallback_nudge.template is not None
        assert "compact_self" in fallback_nudge.template

    def test_condition_references_close_task_and_helpers(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("auto-compact-after-task-close")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)

        assert body.when is not None
        assert "gobby-tasks" in body.when
        assert "close_task" in body.when
        assert "tool_call_succeeded()" in body.when
        assert "task_type_in" in body.when
        assert "claimed_tasks" in body.when
        assert "_auto_compact_after_task_close_queued_for" in body.when

    @pytest.mark.asyncio
    async def test_terminal_close_task_queues_compact_self_once(self, db) -> None:
        _sync_bundled(db)
        engine = RuleEngine(db)
        variables = {
            "claimed_tasks": {
                "task-a": {"task_type": "task"},
                "task-b": {"task_type": "task"},
            }
        }
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="session-1",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": "gobby-tasks",
                    "tool_name": "close_task",
                    "arguments": {"task_id": "#123", "commit_sha": "abc123"},
                },
                "tool_output": {"success": True},
            },
            metadata={"session_type": "terminal"},
        )

        response = await engine.evaluate(event, session_id="session-1", variables=variables)

        compact_calls = [
            call
            for call in response.metadata.get("mcp_calls", [])
            if call["server"] == "gobby-sessions" and call["tool"] == "compact_self"
        ]
        assert len(compact_calls) == 1
        assert compact_calls[0]["background"] is True
        assert compact_calls[0]["arguments"] == {"rule_name": "auto-compact-after-task-close"}
        assert response.context is None
        assert variables["_auto_compact_after_task_close_queued_for"] == "#123"

        second_response = await engine.evaluate(event, session_id="session-1", variables=variables)

        second_compact_calls = [
            call
            for call in second_response.metadata.get("mcp_calls", [])
            if call["server"] == "gobby-sessions" and call["tool"] == "compact_self"
        ]
        assert second_compact_calls == []
