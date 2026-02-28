"""Tests for memory-lifecycle rules.

Verifies memory lifecycle rules sync correctly and have proper structure.
Rules that were merged into context-handoff (preserve-context-on-compact,
preserve-context-on-end) are tested there instead.

Active memory-lifecycle rules:
- reset-memory-tracking-on-start: set_variable on session_start
- memory-sync-import: mcp_call on session_start
- memory-recall-on-prompt: mcp_call on before_agent
- memory-background-digest: mcp_call on before_agent (background)
- memory-capture-nudge: inject_context on before_agent
- suggest-memory-after-close: inject_context on after_tool
- clear-memory-review-on-create: set_variable on before_tool
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

MEMORY_RULES = {
    "reset-memory-tracking-on-start",
    "memory-sync-import",
    "memory-recall-on-prompt",
    "memory-background-digest",
    "memory-capture-nudge",
    "suggest-memory-after-close",
    "clear-memory-review-on-create",
}


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    db_path = tmp_path / "test_memory_lifecycle.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


def _sync_bundled(db):
    """Sync bundled rules from the real rules directory."""
    from gobby.workflows.sync import get_bundled_rules_path

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
                    }


# ═══════════════════════════════════════════════════════════════════════
# reset-memory-tracking-on-start
# ═══════════════════════════════════════════════════════════════════════


class TestResetMemoryTrackingOnStart:
    """Reset injected_memory_ids on context loss (session_start)."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("reset-memory-tracking-on-start", include_templates=True)
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effect.type == "set_variable"
        assert body.effect.variable == "injected_memory_ids"

    def test_has_when_condition(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("reset-memory-tracking-on-start", include_templates=True)
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "clear" in body.when
        assert "compact" in body.when


# ═══════════════════════════════════════════════════════════════════════
# memory-sync-import
# ═══════════════════════════════════════════════════════════════════════


class TestMemorySyncImport:
    """Import memories from JSONL on session_start."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-sync-import", include_templates=True)
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "session_start"
        assert body.effect.type == "mcp_call"
        assert body.effect.server == "gobby-memory"
        assert body.effect.tool == "sync_import"


# ═══════════════════════════════════════════════════════════════════════
# memory-recall-on-prompt
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryRecallOnPrompt:
    """Recall relevant memories before agent prompt."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-recall-on-prompt", include_templates=True)
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_agent"
        assert body.effect.type == "mcp_call"
        assert body.effect.server == "gobby-memory"
        assert body.effect.tool == "digest_and_synthesize"

    def test_not_background(self, db, manager) -> None:
        """Recall must block to inject context."""
        _sync_bundled(db)
        row = manager.get_by_name("memory-recall-on-prompt", include_templates=True)
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effect.background is False


# ═══════════════════════════════════════════════════════════════════════
# memory-background-digest
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryBackgroundDigest:
    """Background digest and synthesize after recall."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-background-digest", include_templates=True)
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_agent"
        assert body.effect.type == "mcp_call"
        assert body.effect.server == "gobby-memory"
        assert body.effect.tool == "digest_and_synthesize"

    def test_is_background(self, db, manager) -> None:
        """Digest runs async (zero latency)."""
        _sync_bundled(db)
        row = manager.get_by_name("memory-background-digest", include_templates=True)
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.effect.background is True


# ═══════════════════════════════════════════════════════════════════════
# memory-capture-nudge
# ═══════════════════════════════════════════════════════════════════════


class TestMemoryCaptureNudge:
    """Nudge agent to save user preferences."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("memory-capture-nudge", include_templates=True)
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_agent"
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None
        assert "create_memory" in body.effect.template

    def test_has_when_condition(self, db, manager) -> None:
        """Only nudge on substantial prompts (not slash commands)."""
        _sync_bundled(db)
        row = manager.get_by_name("memory-capture-nudge", include_templates=True)
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "prompt" in body.when


# ═══════════════════════════════════════════════════════════════════════
# suggest-memory-after-close
# ═══════════════════════════════════════════════════════════════════════


class TestSuggestMemoryAfterClose:
    """Suggest memory extraction after task close with commit."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("suggest-memory-after-close", include_templates=True)
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "after_tool"
        assert body.effect.type == "inject_context"
        assert body.effect.template is not None
        assert "create_memory" in body.effect.template

    def test_has_when_condition(self, db, manager) -> None:
        """Only suggest after close_task with commit_sha."""
        _sync_bundled(db)
        row = manager.get_by_name("suggest-memory-after-close", include_templates=True)
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "close_task" in body.when


# ═══════════════════════════════════════════════════════════════════════
# clear-memory-review-on-create
# ═══════════════════════════════════════════════════════════════════════


class TestClearMemoryReviewOnCreate:
    """Clear pending_memory_review flag when create_memory is called."""

    def test_event_and_effect(self, db, manager) -> None:
        _sync_bundled(db)
        row = manager.get_by_name("clear-memory-review-on-create", include_templates=True)
        assert row is not None
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.event.value == "before_tool"
        assert body.effect.type == "set_variable"
        assert body.effect.variable == "pending_memory_review"
        assert body.effect.value is False

    def test_has_when_condition(self, db, manager) -> None:
        """Must match create_memory on gobby-memory server."""
        _sync_bundled(db)
        row = manager.get_by_name("clear-memory-review-on-create", include_templates=True)
        body = RuleDefinitionBody.model_validate_json(row.definition_json)
        assert body.when is not None
        assert "create_memory" in body.when
        assert "gobby-memory" in body.when
