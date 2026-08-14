"""Tests for tag-aware sync and cascade safety.

Covers:
- Orphan cleanup scoped by tag (gobby vs user)
- Cascade deletion scoped by tag
- Name collision prevention (user can't shadow gobby template)
- install_all_templates with tag filtering
"""

import json

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager

pytestmark = pytest.mark.unit


@pytest.fixture()
def manager(temp_db):
    """Create a workflow definition manager."""
    return RuleDefinitionManager(temp_db)


def _create_rule(manager, name, *, source="installed", tags=None, enabled=False, project_id=None):
    """Helper to create a rule definition row."""
    tags = tags or ["gobby"]
    definition = {
        "event": "before_tool",
        "effects": [{"type": "inject_context", "template": "test"}],
    }
    return manager.create(
        name=name,
        definition_json=json.dumps(definition),
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
        (rules_dir / "retained.yaml").write_text(
            "rules:\n  retained-rule:\n    event: before_tool\n    effect:\n"
            "      type: inject_context\n      template: retained\n"
        )

        result = sync_bundled_rules(temp_db, rules_path=rules_dir)

        # The gobby template should be orphaned
        assert result["orphaned"] >= 1

        # The user template should survive
        all_rows = temp_db.fetchall(
            "SELECT * FROM rule_definitions WHERE name = 'shared-rule' AND deleted_at IS NULL"
        )
        user_rows = [r for r in all_rows if "user" in json.loads(r["tags"] or "[]")]
        assert len(user_rows) == 1, "User-tagged template should survive gobby orphan cleanup"

    def test_gobby_orphan_cleanup_only_targets_gobby_tagged(self, manager, temp_db, tmp_path):
        """Orphan cleanup only soft-deletes installed templates tagged 'gobby'."""
        from gobby.workflows.sync_rules import sync_bundled_rules

        installed = _create_rule(manager, "gobby-only-rule", source="installed", tags=["gobby"])
        duplicate = manager.duplicate(installed.id, "duplicated-rule")
        _create_rule(manager, "custom-gobby-rule", source="custom", tags=["gobby"])
        _create_rule(manager, "user-only-rule", source="installed", tags=["user"])

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "retained.yaml").write_text(
            "rules:\n  retained-rule:\n    event: before_tool\n    effect:\n"
            "      type: inject_context\n      template: retained\n"
        )

        sync_bundled_rules(temp_db, rules_path=rules_dir)

        # gobby template orphaned
        gobby_row = temp_db.fetchone(
            "SELECT deleted_at FROM rule_definitions "
            "WHERE name = 'gobby-only-rule' AND tags::text LIKE '%%gobby%%'"
        )
        assert gobby_row is not None
        assert gobby_row["deleted_at"] is not None

        custom_row = temp_db.fetchone(
            "SELECT deleted_at FROM rule_definitions WHERE name = 'custom-gobby-rule'"
        )
        assert custom_row is not None
        assert custom_row["deleted_at"] is None

        duplicated_row = manager.get(duplicate.id)
        assert duplicated_row.deleted_at is None
        assert duplicated_row.source == "custom"

        # user template untouched
        user_row = temp_db.fetchone(
            "SELECT deleted_at FROM rule_definitions "
            "WHERE name = 'user-only-rule' AND tags::text LIKE '%%user%%'"
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
            "      template: test\n"
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
            "      template: test\n"
        )

        result = sync_bundled_rules(temp_db, rules_path=rules_dir, tag="user")
        assert result["errors"] == []
        assert result["synced"] == 1

        row = temp_db.fetchone("SELECT tags FROM rule_definitions WHERE name = 'my-rule'")
        assert "user" in json.loads(row["tags"])

    def test_user_orphan_does_not_affect_gobby(self, manager, temp_db, tmp_path):
        """User orphan cleanup does not touch gobby templates."""
        from gobby.workflows.sync_rules import sync_bundled_rules

        _create_rule(manager, "gobby-rule", source="installed", tags=["gobby"])
        _create_rule(manager, "old-user-rule", source="installed", tags=["user"])

        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        # A single user root is a partial scan and cannot prove this row is orphaned.

        result = sync_bundled_rules(temp_db, rules_path=rules_dir, tag="user")
        assert result["orphaned"] == 0

        # gobby rule untouched
        gobby = temp_db.fetchone(
            "SELECT deleted_at FROM rule_definitions WHERE name = 'gobby-rule'"
        )
        assert gobby["deleted_at"] is None


class TestMultiRootUserSync:
    def test_consecutive_syncs_preserve_rules_and_variables_from_both_roots(
        self, temp_db, tmp_path, monkeypatch
    ):
        import gobby.paths
        from gobby.cli.installers.shared import _sync_user_templates_to_db

        project_rules = tmp_path / "project-rules"
        global_rules = tmp_path / "global-rules"
        project_variables = tmp_path / "project-variables"
        global_variables = tmp_path / "global-variables"
        for path in (project_rules, global_rules, project_variables, global_variables):
            path.mkdir()

        (project_rules / "project.yaml").write_text(
            "rules:\n  project-rule:\n    event: before_tool\n    effect:\n"
            "      type: inject_context\n      template: project\n"
        )
        (global_rules / "global.yaml").write_text(
            "rules:\n  global-rule:\n    event: before_tool\n    effect:\n"
            "      type: inject_context\n      template: global\n"
        )
        (project_variables / "project.yaml").write_text(
            "variables:\n  project_variable:\n    value: project\n"
        )
        (global_variables / "global.yaml").write_text(
            "variables:\n  global_variable:\n    value: global\n"
        )

        monkeypatch.setattr(gobby.paths, "get_project_rules_dir", lambda _path: project_rules)
        monkeypatch.setattr(gobby.paths, "get_global_rules_dir", lambda: global_rules)
        monkeypatch.setattr(
            gobby.paths, "get_project_variables_dir", lambda _path: project_variables
        )
        monkeypatch.setattr(gobby.paths, "get_global_variables_dir", lambda: global_variables)

        sync_counts = [_sync_user_templates_to_db(temp_db) for _ in range(2)]
        assert sync_counts == [4, 0]

        rule_names = {
            row["name"]
            for row in temp_db.fetchall(
                "SELECT name FROM rule_definitions WHERE deleted_at IS NULL"
            )
        }
        variable_names = {
            row["name"]
            for row in temp_db.fetchall(
                "SELECT name FROM session_variable_defaults WHERE deleted_at IS NULL"
            )
        }
        assert {"project-rule", "global-rule"}.issubset(rule_names)
        assert {"project_variable", "global_variable"}.issubset(variable_names)
        user_tagged = {
            row["name"]
            for row in temp_db.fetchall(
                "SELECT name FROM rule_definitions "
                "WHERE deleted_at IS NULL AND tags::text LIKE '%%user%%'"
            )
        } | {
            row["name"]
            for row in temp_db.fetchall(
                "SELECT name FROM session_variable_defaults "
                "WHERE deleted_at IS NULL AND tags::text LIKE '%%user%%'"
            )
        }
        assert user_tagged == {
            "project-rule",
            "global-rule",
            "project_variable",
            "global_variable",
        }
