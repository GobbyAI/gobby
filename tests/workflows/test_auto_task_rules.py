"""Tests for auto-task rules.

Verifies auto-task rules sync correctly and have proper structure:
- block-in-auto-task-mode: inject_context on before_agent (when auto_task_ref set)
- guide-task-continuation: block on stop (when task tree incomplete)
- notify-task-tree-complete: inject_context on stop (when task tree complete)
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

AUTO_TASK_RULES = {
    "block-in-auto-task-mode",
    "guide-task-continuation",
    "notify-task-tree-complete",
}


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_auto_task.db"
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


class TestAutoTaskSync:
    """Test that auto-task.yaml syncs correctly."""

    def test_bundled_file_syncs_all_rules(self, db, manager) -> None:
        """All 3 auto-task rules should sync to workflow_definitions."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        rule_names = {r.name for r in rules}

        for rule_name in AUTO_TASK_RULES:
            assert rule_name in rule_names, f"Missing rule: {rule_name}"

    def test_all_rules_have_group(self, db, manager) -> None:
        """All auto-task rules should have group='auto-task'."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in AUTO_TASK_RULES:
                body = json.loads(row.definition_json)
                assert body.get("group") == "auto-task", f"{row.name} missing group"

    def test_all_rules_are_valid_pydantic(self, db, manager) -> None:
        """All synced rules should be valid RuleDefinitionBody instances."""
        _sync_bundled(db)

        rules = manager.list_all(workflow_type="rule")
        for row in rules:
            if row.name in AUTO_TASK_RULES:
                body = RuleDefinitionBody.model_validate_json(row.definition_json)
                assert body.effect.type in {
                    "block",
                    "inject_context",
                }


# ═══════════════════════════════════════════════════════════════════════
# block-in-auto-task-mode
# ═══════════════════════════════════════════════════════════════════════


class TestBlockInAutoTaskMode:
    """Inject autonomous mode context when auto_task_ref is set."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("block-in-auto-task-mode")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_agent"
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None
        assert (
            "autonomous" in body.effect.template.lower() or "auto_task_ref" in body.effect.template
        )

    def test_has_auto_task_ref_condition(self, db, manager) -> None:
        """Only inject when auto_task_ref is set."""
        _sync_bundled(db)
        row = manager.get_by_name("block-in-auto-task-mode")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "auto_task_ref" in body.when

    def test_template_mentions_suggest_next_task(self, db, manager) -> None:
        """Template should guide agent to use suggest_next_task."""
        _sync_bundled(db)
        row = manager.get_by_name("block-in-auto-task-mode")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert "suggest_next_task" in body.effect.template


# ═══════════════════════════════════════════════════════════════════════
# guide-task-continuation
# ═══════════════════════════════════════════════════════════════════════


class TestGuideTaskContinuation:
    """Block stop when task tree is incomplete."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("guide-task-continuation")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "stop"
        assert body.effect.type == "block"
        assert body.effect.reason is not None

    def test_has_task_tree_condition(self, db, manager) -> None:
        """Should check task_tree_complete and auto_task_ref."""
        _sync_bundled(db)
        row = manager.get_by_name("guide-task-continuation")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "auto_task_ref" in body.when
        assert "task_tree_complete" in body.when

    def test_has_escape_hatch(self, db, manager) -> None:
        """Should respect max stop attempts for escape hatch."""
        _sync_bundled(db)
        row = manager.get_by_name("guide-task-continuation")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "stop_attempts" in body.when

    def test_reason_guides_continuation(self, db, manager) -> None:
        """Block reason should guide agent to continue working."""
        _sync_bundled(db)
        row = manager.get_by_name("guide-task-continuation")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert "suggest_next_task" in body.effect.reason


# ═══════════════════════════════════════════════════════════════════════
# notify-task-tree-complete
# ═══════════════════════════════════════════════════════════════════════


class TestNotifyTaskTreeComplete:
    """Inject completion notice when task tree is done."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("notify-task-tree-complete")
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "stop"
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None

    def test_has_completion_condition(self, db, manager) -> None:
        """Should check both auto_task_ref set and task_tree_complete."""
        _sync_bundled(db)
        row = manager.get_by_name("notify-task-tree-complete")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "auto_task_ref" in body.when
        assert "task_tree_complete" in body.when

    def test_template_mentions_complete(self, db, manager) -> None:
        """Template should indicate tasks are complete."""
        _sync_bundled(db)
        row = manager.get_by_name("notify-task-tree-complete")
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert "complete" in body.effect.template.lower()
