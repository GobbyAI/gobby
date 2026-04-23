"""Tests for context-handoff rules.

Verifies context handoff rules sync correctly and have proper structure:
- clear-pending-context-reset-on-start: set_variable on session_start
- capture-baseline-dirty-files-on-start: mcp_call on session_start
- inject-previous-session-summary: inject_context on session_start
- inject-compact-handoff: inject_context on session_start
- inject-task-context-on-start: inject_context on session_start
- preserve-context-on-compact: set_variable on pre_compact (reset tracking vars)

"""

from __future__ import annotations

import json

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
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
}


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_context_handoff.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
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
