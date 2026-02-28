"""Tests for context-handoff rules.

Verifies context handoff rules sync correctly and have proper structure:
- clear-pending-context-reset-on-start: set_variable on session_start
- capture-baseline-dirty-files-on-start: mcp_call on session_start
- inject-previous-session-summary: inject_context on session_start
- inject-compact-handoff: inject_context on session_start
- task-sync-import-on-start: mcp_call on session_start
- inject-skills-on-start: inject_context on session_start
- inject-task-context-on-start: inject_context on session_start
- inject-error-triage-policy: inject_context on session_start
- preserve-context-on-end: multi-effect mcp_call on session_end
- preserve-context-on-compact: multi-effect set_variable+mcp_call on pre_compact
"""

from __future__ import annotations

import json

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import RuleDefinitionBody
from gobby.workflows.sync import sync_bundled_rules

pytestmark = pytest.mark.unit

CONTEXT_HANDOFF_RULES = {
    "clear-pending-context-reset-on-start",
    "capture-baseline-dirty-files-on-start",
    "inject-previous-session-summary",
    "inject-compact-handoff",
    "task-sync-import-on-start",
    "inject-task-context-on-start",
    "inject-error-triage-policy",
    "preserve-context-on-end",
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
    from gobby.workflows.sync import get_bundled_rules_path

    result = sync_bundled_rules(db, get_bundled_rules_path())
    # Mark templates as installed so get_by_name() finds them without include_templates
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
        assert body.effect.type == "set_variable"
        assert body.effect.variable == "pending_context_reset"

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
        assert body.effect.type == "mcp_call"
        assert body.effect.server == "gobby-sessions"
        assert body.effect.tool == "capture_baseline_dirty_files"


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
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None
        assert "Previous Session Context" in body.effect.template

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
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None
        assert "Continuation Context" in body.effect.template

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-compact-handoff")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "compact" in body.when


# ═══════════════════════════════════════════════════════════════════════
# task-sync-import-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestTaskSyncImportOnStart:
    """Import tasks from JSONL on session_start."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("task-sync-import-on-start")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effect.type == "mcp_call"
        assert body.effect.server == "gobby-tasks"
        assert body.effect.tool == "sync_import"


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
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-task-context-on-start")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "resume" in body.when


# ═══════════════════════════════════════════════════════════════════════
# inject-error-triage-policy
# ═══════════════════════════════════════════════════════════════════════


class TestInjectErrorTriagePolicy:
    """Inject pre-existing error triage policy on session_start."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-error-triage-policy")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None
        assert "Pre-Existing" in body.effect.template

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("inject-error-triage-policy")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "resume" in body.when


# ═══════════════════════════════════════════════════════════════════════
# preserve-context-on-end (multi-effect)
# ═══════════════════════════════════════════════════════════════════════


class TestPreserveContextOnEnd:
    """Generate handoff summary and export data on session_end (merged rule)."""

    def test_event_is_session_end(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-end")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_end"

    def test_has_four_effects(self, db, manager) -> None:
        """Should have 4 mcp_call effects."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-end")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        assert len(effects) == 4
        for effect in effects:
            assert effect.type == "mcp_call"

    def test_includes_handoff_generation(self, db, manager) -> None:
        """Should include set_handoff_context with full+write_file."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-end")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        handoff_effects = [
            e for e in effects if e.server == "gobby-sessions" and e.tool == "set_handoff_context"
        ]
        assert len(handoff_effects) == 1
        assert handoff_effects[0].arguments.get("full") is True
        assert handoff_effects[0].arguments.get("write_file") is True

    def test_includes_task_sync_export(self, db, manager) -> None:
        """Should include task sync export."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-end")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        task_exports = [e for e in effects if e.server == "gobby-tasks" and e.tool == "sync_export"]
        assert len(task_exports) == 1

    def test_includes_memory_extract_and_export(self, db, manager) -> None:
        """Should include memory extraction and sync export."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-end")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        memory_effects = [e for e in effects if e.server == "gobby-memory"]
        assert len(memory_effects) == 2
        memory_tools = {e.tool for e in memory_effects}
        assert "extract_from_session" in memory_tools
        assert "sync_export" in memory_tools

    def test_no_when_condition(self, db, manager) -> None:
        """Should fire unconditionally on session_end."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-end")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is None


# ═══════════════════════════════════════════════════════════════════════
# preserve-context-on-compact (multi-effect)
# ═══════════════════════════════════════════════════════════════════════


class TestPreserveContextOnCompact:
    """Reset tracking, extract context, and export data before compaction (merged rule)."""

    def test_event_is_pre_compact(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "pre_compact"

    def test_has_seven_effects(self, db, manager) -> None:
        """Should have 7 effects (2 set_variable + 5 mcp_call)."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        assert len(effects) == 7
        set_var_effects = [e for e in effects if e.type == "set_variable"]
        mcp_effects = [e for e in effects if e.type == "mcp_call"]
        assert len(set_var_effects) == 2
        assert len(mcp_effects) == 5

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

    def test_includes_handoff_generation(self, db, manager) -> None:
        """Should include set_handoff_context with full+write_file."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        handoff_effects = [
            e for e in effects if e.server == "gobby-sessions" and e.tool == "set_handoff_context"
        ]
        # One compact handoff + one full handoff
        assert len(handoff_effects) == 2
        full_handoff = [
            e for e in handoff_effects if e.arguments and e.arguments.get("full") is True
        ]
        assert len(full_handoff) == 1
        assert full_handoff[0].arguments.get("write_file") is True

    def test_includes_task_sync_export(self, db, manager) -> None:
        """Should include task sync export."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        task_exports = [e for e in effects if e.server == "gobby-tasks" and e.tool == "sync_export"]
        assert len(task_exports) == 1

    def test_includes_memory_extract_and_export(self, db, manager) -> None:
        """Should include memory extraction and sync export."""
        _sync_bundled(db)
        row = manager.get_by_name("preserve-context-on-compact")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        effects = body.resolved_effects
        memory_effects = [e for e in effects if e.server == "gobby-memory"]
        assert len(memory_effects) == 2
        memory_tools = {e.tool for e in memory_effects}
        assert "extract_from_session" in memory_tools
        assert "sync_export" in memory_tools
