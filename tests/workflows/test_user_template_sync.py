"""Tests for tag-aware sync and cascade safety.

Covers:
- Orphan cleanup scoped by tag (gobby vs user)
- Cascade deletion scoped by tag
- Name collision prevention (user can't shadow gobby template)
- install_all_templates with tag filtering
"""

import json

import pytest

from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager

pytestmark = pytest.mark.unit


@pytest.fixture()
def manager(temp_db):
    """Create a workflow definition manager."""
    return LocalWorkflowDefinitionManager(temp_db)


def _create_rule(manager, name, *, source="installed", tags=None, enabled=False, project_id=None):
    """Helper to create a rule definition row."""
    tags = tags or ["gobby"]
    definition = {
        "event": "before_tool",
        "effects": [{"type": "inject_context", "content": "test"}],
    }
    return manager.create(
        name=name,
        definition_json=json.dumps(definition),
        workflow_type="rule",
        source=source,
        tags=tags,
        enabled=enabled,
        project_id=project_id,
    )


class TestOrphanTagIsolation:
    """Orphan cleanup should only affect rows with matching tags."""

    def test_gobby_orphan_does_not_delete_user_template(
        self, manager, temp_db, tmp_path, sample_project
    ):
        """When a gobby-tagged template is orphaned, user-tagged templates
        with the same name survive."""
        from gobby.workflows.sync_rules import sync_bundled_rules

        # Create a gobby template (global) and a user template (project-scoped)
        # They can share a name because they have different project_ids
        _create_rule(manager, "shared-rule", source="installed", tags=["gobby"])
        _create_rule(
            manager,
            "shared-rule",
            source="installed",
            tags=["user"],
            project_id=sample_project["id"],
        )

        # Sync with empty rules dir — gobby template becomes orphan
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        result = sync_bundled_rules(temp_db, rules_path=rules_dir)

        # The gobby template should be orphaned
        assert result["orphaned"] >= 1

        # The user template should survive
        all_rows = temp_db.fetchall(
            "SELECT * FROM workflow_definitions WHERE name = 'shared-rule' AND deleted_at IS NULL"
        )
        user_rows = [r for r in all_rows if "user" in json.loads(r["tags"] or "[]")]
        assert len(user_rows) == 1, "User-tagged template should survive gobby orphan cleanup"

    def test_gobby_orphan_cleanup_only_targets_gobby_tagged(self, manager, temp_db, tmp_path):
        """Orphan cleanup only soft-deletes templates tagged 'gobby'."""
        from gobby.workflows.sync_rules import sync_bundled_rules

        _create_rule(manager, "gobby-only-rule", source="installed", tags=["gobby"])
        _create_rule(manager, "user-only-rule", source="installed", tags=["user"])

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()

        sync_bundled_rules(temp_db, rules_path=rules_dir)

        # gobby template orphaned
        gobby_row = temp_db.fetchone(
            "SELECT deleted_at FROM workflow_definitions WHERE name = 'gobby-only-rule' AND tags LIKE '%gobby%'"
        )
        assert gobby_row is not None
        assert gobby_row["deleted_at"] is not None

        # user template untouched
        user_row = temp_db.fetchone(
            "SELECT deleted_at FROM workflow_definitions WHERE name = 'user-only-rule' AND tags LIKE '%user%'"
        )
        assert user_row is not None
        assert user_row["deleted_at"] is None


class TestNameCollisionPrevention:
    """User templates should not shadow bundled gobby templates."""

    def test_user_sync_skips_gobby_named_template(self, manager, temp_db, tmp_path):
        """sync_user_rules should skip rules whose names match gobby templates."""
        from gobby.workflows.sync_rules import sync_bundled_rules

        # Create a gobby template
        _create_rule(manager, "protected-rule", source="installed", tags=["gobby"])

        # Write a user rule YAML with the same name
        rules_dir = tmp_path / "user_rules"
        rules_dir.mkdir()
        (rules_dir / "protected-rule.yaml").write_text(
            "rules:\n"
            "  protected-rule:\n"
            "    event: before_tool\n"
            "    effect:\n"
            "      type: inject_context\n"
            "      content: test\n"
        )

        # Sync user rules — should skip collision
        result = sync_bundled_rules(temp_db, rules_path=rules_dir, tag="user")
        assert result["skipped"] >= 1


class TestSyncUserRules:
    """Tests for syncing user-created rules from .gobby/workflows/rules/."""

    def test_sync_user_rules_creates_with_user_tag(self, manager, temp_db, tmp_path):
        """User rule sync creates templates with tags=['user']."""
        from gobby.workflows.sync_rules import sync_bundled_rules

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "my-rule.yaml").write_text(
            "rules:\n"
            "  my-rule:\n"
            "    event: before_tool\n"
            "    effect:\n"
            "      type: inject_context\n"
            "      content: test\n"
        )

        result = sync_bundled_rules(temp_db, rules_path=rules_dir, tag="user")
        assert result["errors"] == []
        assert result["synced"] == 1

        row = temp_db.fetchone("SELECT tags FROM workflow_definitions WHERE name = 'my-rule'")
        assert "user" in json.loads(row["tags"])

    def test_user_orphan_does_not_affect_gobby(self, manager, temp_db, tmp_path):
        """User orphan cleanup does not touch gobby templates."""
        from gobby.workflows.sync_rules import sync_bundled_rules

        _create_rule(manager, "gobby-rule", source="installed", tags=["gobby"])
        _create_rule(manager, "old-user-rule", source="installed", tags=["user"])

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        # Empty dir — old-user-rule is orphaned, but gobby-rule should not be

        result = sync_bundled_rules(temp_db, rules_path=rules_dir, tag="user")
        assert result["orphaned"] == 1

        # gobby rule untouched
        gobby = temp_db.fetchone(
            "SELECT deleted_at FROM workflow_definitions WHERE name = 'gobby-rule'"
        )
        assert gobby["deleted_at"] is None
