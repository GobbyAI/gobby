"""Tests for syncing typed rule YAML files into rule_definitions rows.

Tests syncing rule YAML files (with `rules:` key and event/effect format)
into rule_definitions rows.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._rules import update_rule
from gobby.storage.definitions import DefinitionNotFoundError
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.sync_rules import (
    _iter_active_rule_files,
    get_bundled_rules_paths,
    sync_bundled_rules,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    """Create a fresh database with migrations applied."""
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(db)


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    """Create a temporary rules directory."""
    d = tmp_path / "rules"
    d.mkdir()
    return d


class TestSingleRuleYaml:
    """Parse a single-rule YAML file."""

    def test_single_rule_synced(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """A YAML file with one rule should create one rule_definitions row."""
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

        rows = manager.list_all()
        assert len(rows) == 1
        assert rows[0].name == "no-push"
        assert rows[0].source == "installed"

    def test_rule_definition_json_is_valid(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
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

        rows = manager.list_all()
        body = rows[0].definition_json
        assert body["event"] == "before_tool"
        assert body["effects"][0]["type"] == "block"
        assert body["effects"][0]["tools"] == ["Edit"]

    def test_rule_enabled_defaults_true(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """Bundled rules are enabled by default."""
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

        rows = manager.list_all()
        assert rows[0].enabled is True

    def test_turn_end_rule_syncs(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """Semantic turn_end rules should sync like any other rule event."""
        (rules_dir / "turn-end.yaml").write_text(
            """
rules:
  loop-turn-end:
    event: turn_end
    effect:
      type: block
      reason: "Keep going."
"""
        )

        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 1
        rows = manager.list_all()
        body = rows[0].definition_json
        assert body["event"] == "turn_end"


class TestMultiRuleYamlWithDefaults:
    """Parse multi-rule YAML with file-level defaults."""

    def test_multiple_rules_from_one_file(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """Multiple rules in one YAML file should create multiple rows."""
        (rules_dir / "multi.yaml").write_text(
            """
group: tool-hygiene
tags: [enforcement]
sources: [claude, qwen]

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
        rows = manager.list_all()
        names = sorted(r.name for r in rows)
        assert names == ["rule-a", "rule-b"]

    def test_file_level_group_inherited(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
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

        rows = manager.list_all()
        body = rows[0].definition_json
        assert body["group"] == "safety-rules"

    def test_file_level_sources_inherited(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """File-level sources should be set on the rule_definitions row."""
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

        rows = manager.list_all()
        assert rows[0].sources is not None
        sources = (
            json.loads(rows[0].sources) if isinstance(rows[0].sources, str) else rows[0].sources
        )
        assert "claude" in sources
        assert "codex" in sources

    def test_file_level_tags_inherited(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """File-level tags should be set on the rule_definitions row."""
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

        rows = manager.list_all()
        assert rows[0].tags is not None

    def test_rule_without_tags_uses_empty_list(self) -> None:
        from unittest.mock import MagicMock

        from gobby.workflows.sync_rules import _sync_single_rule

        manager = MagicMock()
        manager.get_by_name.return_value = None
        result = {"synced": 0, "updated": 0, "skipped": 0}
        _sync_single_rule(
            manager=manager,
            rule_name="my-rule",
            rule_data={
                "event": "before_tool",
                "effect": {"type": "block", "reason": "Blocked."},
            },
            file_group=None,
            file_tags=None,
            file_sources=None,
            file_audience=None,
            sync_tag="gobby",
            result=result,
        )

        assert manager.create.call_args.kwargs["tags"] == []

    def test_rule_level_priority_overrides_default(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
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

        rows = manager.list_all()
        by_name = {r.name: r for r in rows}
        assert by_name["high-pri"].priority == 10
        assert by_name["low-pri"].priority == 90


class TestUpsertOnResync:
    """Re-running sync should update changed rules, skip unchanged."""

    def test_unchanged_rule_skipped(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
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

    def test_enabled_default_flip_updates_unmodified_rule(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        rule_file = rules_dir / "changing.yaml"
        rule_file.write_text(
            """
rules:
  mutable-rule:
    event: before_tool
    enabled: false
    effect:
      type: block
      reason: "Still disabled by default."
"""
        )
        sync_bundled_rules(db, rules_dir)

        seeded = manager.get_by_name("mutable-rule")
        assert seeded is not None
        assert seeded.enabled is False
        assert seeded.enabled_pinned is False

        rule_file.write_text(
            """
rules:
  mutable-rule:
    event: before_tool
    enabled: true
    effect:
      type: block
      reason: "Now enabled by default."
"""
        )
        result = sync_bundled_rules(db, rules_dir)

        updated = manager.get_by_name("mutable-rule")
        assert updated is not None
        assert result["updated"] == 1
        assert updated.enabled is True
        assert updated.enabled_pinned is False

    def test_enabled_default_flip_preserves_explicit_user_disable(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        rule_file = rules_dir / "changing.yaml"
        rule_file.write_text(
            """
rules:
  mutable-rule:
    event: before_tool
    enabled: false
    effect:
      type: block
      reason: "Still disabled by default."
"""
        )
        sync_bundled_rules(db, rules_dir)

        response = update_rule(manager, "mutable-rule", enabled=False)
        assert response["success"] is True
        explicitly_disabled = manager.get_by_name("mutable-rule")
        assert explicitly_disabled is not None
        assert explicitly_disabled.enabled_pinned is True

        rule_file.write_text(
            """
rules:
  mutable-rule:
    event: before_tool
    enabled: true
    effect:
      type: block
      reason: "Now enabled by default."
"""
        )
        sync_bundled_rules(db, rules_dir)

        preserved = manager.get_by_name("mutable-rule")
        assert preserved is not None
        assert preserved.enabled is False
        assert preserved.enabled_pinned is True

    def test_message_only_change_updates_reason_and_preserves_enabled_toggle(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """Syncing message drift should update the row and retain the operator toggle."""
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

        rows = manager.list_all()
        body1 = rows[0].definition_json
        assert body1["effects"][0]["reason"] == "Version 1."
        original_enabled = rows[0].enabled

        manager.update(rows[0].id, enabled=False)

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

        # Bundled row is refreshed, but user toggle is preserved.
        rows = manager.list_all()
        body2 = rows[0].definition_json
        assert body2["effects"][0]["reason"] == "Version 2."
        assert original_enabled is True
        assert rows[0].enabled is False

    def test_user_or_custom_rule_not_overwritten(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """Only bundled gobby rows should be refreshed on re-sync."""
        manager.create(
            name="user-owned-rule",
            definition_json=json.dumps(
                {
                    "event": "stop",
                    "effects": [{"type": "block", "reason": "Keep original user rule"}],
                }
            ),
            source="installed",
            tags=["user"],
            enabled=True,
        )
        manager.create(
            name="custom-owned-rule",
            definition_json=json.dumps(
                {
                    "event": "stop",
                    "effects": [{"type": "block", "reason": "Keep original custom rule"}],
                }
            ),
            source="custom",
            tags=["gobby"],
            enabled=True,
        )

        (rules_dir / "protected.yaml").write_text(
            """
rules:
  user-owned-rule:
    event: turn_end
    effect:
      type: block
      reason: "bundled replacement"
  custom-owned-rule:
    event: turn_end
    effect:
      type: block
      reason: "bundled replacement"
"""
        )

        result = sync_bundled_rules(db, rules_dir)

        assert result["updated"] == 0
        assert result["skipped"] == 2

        user_row = manager.get_by_name("user-owned-rule")
        custom_row = manager.get_by_name("custom-owned-rule")
        assert user_row is not None
        assert custom_row is not None
        assert user_row.definition_json["event"] == "stop"
        assert custom_row.definition_json["event"] == "stop"

    def test_soft_deleted_template_restored_on_resync(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
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

        rows = manager.list_all()
        manager.delete(rows[0].id)

        # Verify it's soft-deleted
        deleted = manager.get_by_name("delete-me", include_deleted=True)
        assert deleted is not None
        assert deleted.deleted_at is not None

        # Re-sync restores the bundled definition.
        result2 = sync_bundled_rules(db, rules_dir)
        assert result2["updated"] == 1
        assert manager.get_by_name("delete-me") is not None
        assert result2["synced"] == 0

        restored = manager.get_by_name("delete-me", include_deleted=True)
        assert restored is not None
        assert restored.deleted_at is None

    def test_soft_deleted_user_and_custom_rows_adopted_on_sync(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
        """A soft-deleted user/custom row no longer blocks bundled adoption."""
        user_row = manager.create(
            name="dead-user-rule",
            definition_json=json.dumps(
                {
                    "event": "stop",
                    "effects": [{"type": "block", "reason": "old user rule"}],
                }
            ),
            source="installed",
            tags=["user"],
        )
        custom_row = manager.create(
            name="dead-custom-rule",
            definition_json=json.dumps(
                {
                    "event": "stop",
                    "effects": [{"type": "block", "reason": "old custom rule"}],
                }
            ),
            source="custom",
            tags=["gobby"],
        )
        manager.delete(user_row.id)
        manager.delete(custom_row.id)

        (rules_dir / "adopted.yaml").write_text(
            """
rules:
  dead-user-rule:
    event: turn_end
    effect:
      type: block
      reason: "bundled adoption"
  dead-custom-rule:
    event: turn_end
    effect:
      type: block
      reason: "bundled adoption"
"""
        )

        result = sync_bundled_rules(db, rules_dir)

        assert result["synced"] == 2
        assert result["skipped"] == 0
        for name, old_id in (
            ("dead-user-rule", user_row.id),
            ("dead-custom-rule", custom_row.id),
        ):
            adopted = manager.get_by_name(name, include_deleted=True)
            assert adopted is not None
            assert adopted.deleted_at is None
            assert adopted.id != old_id
            assert adopted.source == "installed"
            assert "gobby" in (adopted.tags or [])
            assert adopted.definition_json["event"] == "turn_end"
            with pytest.raises(DefinitionNotFoundError):
                manager.get(old_id, include_deleted=True)


class TestInvalidRuleYaml:
    """Invalid YAML should be skipped with errors logged."""

    def test_missing_event_field_skipped(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
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

    def test_missing_effect_field_skipped(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
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

    def test_non_rule_yaml_ignored(self, db: HubDatabase, rules_dir: Path) -> None:
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

    def test_rules_from_multiple_files(
        self, db: HubDatabase, manager: RuleDefinitionManager, rules_dir: Path
    ) -> None:
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
        rows = manager.list_all()
        names = sorted(r.name for r in rows)
        assert names == ["rule-from-file1", "rule-from-file2"]


class TestBundledRuleRoots:
    """Every bundled template must expose its rules to the sync scan."""

    def test_every_bundled_rule_file_declares_rules_mapping(self) -> None:
        roots = [root for root in get_bundled_rules_paths() if root.exists()]
        scanned = _iter_active_rule_files(roots)
        offenders: list[str] = []
        for root, yaml_file in scanned:
            data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
            rules = data.get("rules") if isinstance(data, dict) else None
            if not isinstance(rules, dict) or not rules:
                offenders.append(yaml_file.relative_to(root).as_posix())

        assert scanned
        assert offenders == []

    def test_bundled_file_without_rules_mapping_warns(
        self,
        db: HubDatabase,
        rules_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (rules_dir / "broken.yaml").write_text(
            "broken-rule:\n  event: before_tool\n  effect:\n    type: block\n",
            encoding="utf-8",
        )

        with caplog.at_level(logging.DEBUG, logger="gobby.workflows.sync_rules"):
            result = sync_bundled_rules(db, rules_dir)

        assert result["skipped"] == 1
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "broken.yaml" in warnings[0].getMessage()

    def test_user_root_without_rules_mapping_stays_debug(
        self,
        db: HubDatabase,
        rules_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (rules_dir / "custom.yaml").write_text("notes: personal\n", encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger="gobby.workflows.sync_rules"):
            result = sync_bundled_rules(db, rules_dir, tag="user")

        assert result["skipped"] == 1
        skip_records = [record for record in caplog.records if "custom.yaml" in record.getMessage()]
        assert [record.levelno for record in skip_records] == [logging.DEBUG]
