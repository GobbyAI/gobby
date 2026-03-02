"""Tests for new rule YAML sync to workflow_definitions table.

Tests syncing rule YAML files (with `rules:` key and event/effect format)
into workflow_definitions rows with workflow_type='rule'.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.sync import sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def db(tmp_path) -> LocalDatabase:
    """Create a fresh database with migrations applied."""
    db_path = tmp_path / "test_rule_yaml_sync.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def manager(db: LocalDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def rules_dir(tmp_path) -> Path:
    """Create a temporary rules directory."""
    d = tmp_path / "rules"
    d.mkdir()
    return d


class TestSingleRuleYaml:
    """Parse a single-rule YAML file."""

    def test_single_rule_synced(self, db, manager, rules_dir) -> None:
        """A YAML file with one rule should create one workflow_definitions row."""
        (rules_dir / "simple.yaml").write_text(
            """
rules:
  no-push:
    event: before_tool
    effect:
      type: block
      tools: [Bash]
      command_pattern: "git\\\\s+push"
      reason: "No pushing allowed."
"""
        )
        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 1
        assert result["errors"] == []

        rows = manager.list_all(workflow_type="rule")
        assert len(rows) == 1
        assert rows[0].name == "no-push"
        assert rows[0].workflow_type == "rule"
        assert rows[0].source == "template"

    def test_rule_definition_json_is_valid(self, db, manager, rules_dir) -> None:
        """The stored definition_json should be a valid RuleDefinitionBody."""
        (rules_dir / "simple.yaml").write_text(
            """
rules:
  block-edit:
    event: before_tool
    effect:
      type: block
      tools: [Edit]
      reason: "Blocked."
"""
        )
        sync_bundled_rules(db, rules_dir)

        rows = manager.list_all(workflow_type="rule")
        body = json.loads(rows[0].definition_json)
        assert body["event"] == "before_tool"
        assert body["effect"]["type"] == "block"
        assert body["effect"]["tools"] == ["Edit"]

    def test_rule_enabled_defaults_false(self, db, manager, rules_dir) -> None:
        """Rules should be disabled by default (opt-in activation)."""
        (rules_dir / "simple.yaml").write_text(
            """
rules:
  my-rule:
    event: before_tool
    effect:
      type: block
      reason: "No."
"""
        )
        sync_bundled_rules(db, rules_dir)

        rows = manager.list_all(workflow_type="rule")
        assert rows[0].enabled is False


class TestMultiRuleYamlWithDefaults:
    """Parse multi-rule YAML with file-level defaults."""

    def test_multiple_rules_from_one_file(self, db, manager, rules_dir) -> None:
        """Multiple rules in one YAML file should create multiple rows."""
        (rules_dir / "multi.yaml").write_text(
            """
group: tool-hygiene
tags: [enforcement]
sources: [claude, gemini]

rules:
  rule-a:
    event: before_tool
    effect:
      type: block
      reason: "A"

  rule-b:
    event: after_tool
    effect:
      type: set_variable
      variable: foo
      value: true
"""
        )
        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 2
        rows = manager.list_all(workflow_type="rule")
        names = sorted(r.name for r in rows)
        assert names == ["rule-a", "rule-b"]

    def test_file_level_group_inherited(self, db, manager, rules_dir) -> None:
        """File-level group should be inherited by each rule."""
        (rules_dir / "grouped.yaml").write_text(
            """
group: safety-rules

rules:
  my-rule:
    event: before_tool
    effect:
      type: block
      reason: "Safe."
"""
        )
        sync_bundled_rules(db, rules_dir)

        rows = manager.list_all(workflow_type="rule")
        body = json.loads(rows[0].definition_json)
        assert body["group"] == "safety-rules"

    def test_file_level_sources_inherited(self, db, manager, rules_dir) -> None:
        """File-level sources should be set on the workflow_definitions row."""
        (rules_dir / "sourced.yaml").write_text(
            """
sources: [claude, codex]

rules:
  my-rule:
    event: before_tool
    effect:
      type: block
      reason: "Blocked."
"""
        )
        sync_bundled_rules(db, rules_dir)

        rows = manager.list_all(workflow_type="rule")
        assert rows[0].sources is not None
        sources = (
            json.loads(rows[0].sources) if isinstance(rows[0].sources, str) else rows[0].sources
        )
        assert "claude" in sources
        assert "codex" in sources

    def test_file_level_tags_inherited(self, db, manager, rules_dir) -> None:
        """File-level tags should be set on the workflow_definitions row."""
        (rules_dir / "tagged.yaml").write_text(
            """
tags: [enforcement, python]

rules:
  my-rule:
    event: before_tool
    effect:
      type: block
      reason: "Blocked."
"""
        )
        sync_bundled_rules(db, rules_dir)

        rows = manager.list_all(workflow_type="rule")
        assert rows[0].tags is not None

    def test_rule_level_priority_overrides_default(self, db, manager, rules_dir) -> None:
        """Rule-level priority should override file-level default."""
        (rules_dir / "priority.yaml").write_text(
            """
rules:
  high-pri:
    event: before_tool
    priority: 10
    effect:
      type: block
      reason: "High priority."

  low-pri:
    event: before_tool
    priority: 90
    effect:
      type: block
      reason: "Low priority."
"""
        )
        sync_bundled_rules(db, rules_dir)

        rows = manager.list_all(workflow_type="rule")
        by_name = {r.name: r for r in rows}
        assert by_name["high-pri"].priority == 10
        assert by_name["low-pri"].priority == 90


class TestUpsertOnResync:
    """Re-running sync should update changed rules, skip unchanged."""

    def test_unchanged_rule_skipped(self, db, manager, rules_dir) -> None:
        """Syncing the same rule twice should skip on second run."""
        yaml_content = """
rules:
  stable-rule:
    event: before_tool
    effect:
      type: block
      reason: "Stable."
"""
        (rules_dir / "stable.yaml").write_text(yaml_content)

        result1 = sync_bundled_rules(db, rules_dir)
        assert result1["synced"] == 1

        result2 = sync_bundled_rules(db, rules_dir)
        assert result2["synced"] == 0
        assert result2["skipped"] == 1

    def test_changed_rule_updated(self, db, manager, rules_dir) -> None:
        """Syncing a changed rule should update the row."""
        (rules_dir / "changing.yaml").write_text(
            """
rules:
  mutable-rule:
    event: before_tool
    effect:
      type: block
      reason: "Version 1."
"""
        )
        sync_bundled_rules(db, rules_dir)

        rows = manager.list_all(workflow_type="rule")
        body1 = json.loads(rows[0].definition_json)
        assert body1["effect"]["reason"] == "Version 1."

        (rules_dir / "changing.yaml").write_text(
            """
rules:
  mutable-rule:
    event: before_tool
    effect:
      type: block
      reason: "Version 2."
"""
        )
        result2 = sync_bundled_rules(db, rules_dir)
        assert result2["updated"] == 1

        rows = manager.list_all(workflow_type="rule")
        body2 = json.loads(rows[0].definition_json)
        assert body2["effect"]["reason"] == "Version 2."

    def test_soft_deleted_template_restored_on_resync(self, db, manager, rules_dir) -> None:
        """A soft-deleted template rule should be restored on re-sync."""
        (rules_dir / "deletable.yaml").write_text(
            """
rules:
  delete-me:
    event: before_tool
    effect:
      type: block
      reason: "Delete me."
"""
        )
        sync_bundled_rules(db, rules_dir)

        rows = manager.list_all(workflow_type="rule")
        manager.delete(rows[0].id)

        # Verify it's soft-deleted
        deleted = manager.get_by_name("delete-me", include_deleted=True, include_templates=True)
        assert deleted is not None
        assert deleted.deleted_at is not None

        result2 = sync_bundled_rules(db, rules_dir)
        assert result2["updated"] == 1
        assert result2["skipped"] == 0

        # Verify it's restored (disabled)
        restored = manager.get_by_name("delete-me", include_deleted=False, include_templates=True)
        assert restored is not None
        assert restored.deleted_at is None
        assert restored.enabled is False


class TestInvalidRuleYaml:
    """Invalid YAML should be skipped with errors logged."""

    def test_missing_event_field_skipped(self, db, manager, rules_dir) -> None:
        """A rule without 'event' should be skipped."""
        (rules_dir / "bad.yaml").write_text(
            """
rules:
  bad-rule:
    effect:
      type: block
      reason: "No event."
"""
        )
        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 0
        assert len(result["errors"]) > 0

    def test_missing_effect_field_skipped(self, db, manager, rules_dir) -> None:
        """A rule without 'effect' should be skipped."""
        (rules_dir / "bad2.yaml").write_text(
            """
rules:
  bad-rule:
    event: before_tool
"""
        )
        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 0
        assert len(result["errors"]) > 0

    def test_non_rule_yaml_ignored(self, db, rules_dir) -> None:
        """YAML files without 'rules' key should be ignored."""
        (rules_dir / "not-a-rule.yaml").write_text(
            """
name: some-workflow
type: pipeline
steps: []
"""
        )
        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 0
        assert result["skipped"] == 1


class TestMultipleFiles:
    """Multiple rule YAML files should all be synced."""

    def test_rules_from_multiple_files(self, db, manager, rules_dir) -> None:
        """Rules from different YAML files should all be synced."""
        (rules_dir / "file1.yaml").write_text(
            """
rules:
  rule-from-file1:
    event: before_tool
    effect:
      type: block
      reason: "File 1."
"""
        )
        (rules_dir / "file2.yaml").write_text(
            """
rules:
  rule-from-file2:
    event: stop
    effect:
      type: inject_context
      template: "From file 2."
"""
        )
        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 2
        rows = manager.list_all(workflow_type="rule")
        names = sorted(r.name for r in rows)
        assert names == ["rule-from-file1", "rule-from-file2"]
