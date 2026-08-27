"""Tests for tag-aware sync and cascade safety.

Covers:
- Orphan cleanup scoped by tag (gobby vs user)
- Cascade deletion scoped by tag
- Name collision prevention (user can't shadow gobby template)
- install_all_templates with tag filtering
"""

import json
from pathlib import Path
from typing import Literal

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager

pytestmark = pytest.mark.unit


@pytest.fixture()
def manager(temp_db):
    """Create a workflow definition manager."""
    return RuleDefinitionManager(temp_db)


def _create_rule(
    manager: RuleDefinitionManager,
    name: str,
    *,
    source: Literal["installed", "custom", "project"] = "installed",
    tags: list[str] | None = None,
    enabled: bool = False,
    project_id: str | None = None,
) -> RuleDefinitionRow:
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
        import gobby.utils.project_context
        from gobby.cli.installers.shared import _sync_user_templates_to_db

        # The test cwd is a registered Gobby checkout; keep this run unscoped.
        monkeypatch.setattr(
            gobby.utils.project_context, "get_project_context", lambda cwd=None: None
        )
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


_RULE_YAML = (
    "rules:\n  {name}:\n    event: before_tool\n    effect:\n"
    "      type: inject_context\n      template: {name}\n"
)


def _write_rule(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.yaml").write_text(_RULE_YAML.format(name=name))


def _register_project(temp_db: HubDatabase, name: str) -> str:
    return LocalProjectManager(temp_db).create(name).id


def _rule_scope(temp_db: HubDatabase, name: str) -> str | None:
    """Return the live row's project id, "global" for a global row, None when absent."""
    row = temp_db.fetchone(
        "SELECT project_id FROM rule_definitions WHERE name = %s AND deleted_at IS NULL",
        (name,),
    )
    if row is None:
        return None
    return "global" if row["project_id"] is None else str(row["project_id"])


class TestProjectScopedUserSync:
    def test_project_root_rows_belong_to_the_project_and_global_root_stays_global(
        self, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        project_id = _register_project(temp_db, "scoped-rules")
        project_rules = tmp_path / "project-rules"
        global_rules = tmp_path / "global-rules"
        _write_rule(project_rules, "project-rule")
        _write_rule(global_rules, "global-rule")

        result = sync_bundled_rules(
            temp_db,
            rules_path=[project_rules, global_rules],
            tag="user",
            project_id=project_id,
            project_root=project_rules,
        )

        assert result["errors"] == []
        assert result["synced"] == 2
        assert _rule_scope(temp_db, "project-rule") == project_id
        assert _rule_scope(temp_db, "global-rule") == "global"

    def test_orphan_pruning_never_touches_another_projects_rows(
        self, manager: RuleDefinitionManager, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        from gobby.workflows.sync_rules import sync_bundled_rules

        project_id = _register_project(temp_db, "scoped-rules")
        other_project_id = _register_project(temp_db, "other-project")
        project_rules = tmp_path / "project-rules"
        global_rules = tmp_path / "global-rules"
        _write_rule(project_rules, "project-rule")
        _write_rule(global_rules, "global-rule")
        _create_rule(manager, "foreign-rule", tags=["user"], project_id=other_project_id)
        _create_rule(manager, "stale-project-rule", tags=["user"], project_id=project_id)
        _create_rule(manager, "stale-global-rule", tags=["user"])

        result = sync_bundled_rules(
            temp_db,
            rules_path=[project_rules, global_rules],
            tag="user",
            project_id=project_id,
            project_root=project_rules,
        )

        assert result["errors"] == []
        assert result["orphaned"] == 2
        assert _rule_scope(temp_db, "foreign-rule") == other_project_id
        assert _rule_scope(temp_db, "stale-project-rule") is None
        assert _rule_scope(temp_db, "stale-global-rule") is None

    def test_resync_rescopes_a_global_user_row_to_the_project(
        self, manager: RuleDefinitionManager, temp_db: HubDatabase, tmp_path: Path
    ) -> None:
        """Rows synced before project scoping existed adopt the project on the next sync."""
        from gobby.workflows.sync_rules import sync_bundled_rules

        project_id = _register_project(temp_db, "scoped-rules")
        project_rules = tmp_path / "project-rules"
        _write_rule(project_rules, "project-rule")
        legacy = _create_rule(manager, "project-rule", tags=["user"], enabled=True)

        result = sync_bundled_rules(
            temp_db,
            rules_path=[project_rules],
            tag="user",
            project_id=project_id,
            project_root=project_rules,
        )

        assert result["errors"] == []
        assert result["updated"] == 1
        assert result["synced"] == 0
        refreshed = manager.get(legacy.id)
        assert refreshed is not None
        assert refreshed.project_id == project_id
        assert refreshed.enabled is True

    def test_installer_scopes_the_project_rules_dir_to_the_registered_project(
        self, temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gobby.paths
        import gobby.utils.project_context
        from gobby.cli.installers.shared import _sync_user_templates_to_db

        project_id = _register_project(temp_db, "scoped-rules")
        project_rules = tmp_path / "project-rules"
        global_rules = tmp_path / "global-rules"
        _write_rule(project_rules, "project-rule")
        _write_rule(global_rules, "global-rule")
        monkeypatch.setattr(gobby.paths, "get_project_rules_dir", lambda _path: project_rules)
        monkeypatch.setattr(gobby.paths, "get_global_rules_dir", lambda: global_rules)
        monkeypatch.setattr(
            gobby.paths, "get_project_variables_dir", lambda _path: tmp_path / "no-variables"
        )
        monkeypatch.setattr(
            gobby.paths, "get_global_variables_dir", lambda: tmp_path / "no-global-variables"
        )
        monkeypatch.setattr(
            gobby.utils.project_context,
            "get_project_context",
            lambda cwd=None: {"id": project_id, "project_path": str(cwd)},
        )

        assert _sync_user_templates_to_db(temp_db) == 2
        assert _rule_scope(temp_db, "project-rule") == project_id
        assert _rule_scope(temp_db, "global-rule") == "global"

    def test_installer_keeps_rules_global_for_an_unregistered_project(
        self, temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import gobby.paths
        import gobby.utils.project_context
        from gobby.cli.installers.shared import _sync_user_templates_to_db

        project_rules = tmp_path / "project-rules"
        _write_rule(project_rules, "project-rule")
        monkeypatch.setattr(gobby.paths, "get_project_rules_dir", lambda _path: project_rules)
        monkeypatch.setattr(gobby.paths, "get_global_rules_dir", lambda: tmp_path / "no-global")
        monkeypatch.setattr(
            gobby.paths, "get_project_variables_dir", lambda _path: tmp_path / "no-variables"
        )
        monkeypatch.setattr(
            gobby.paths, "get_global_variables_dir", lambda: tmp_path / "no-global-variables"
        )
        monkeypatch.setattr(
            gobby.utils.project_context,
            "get_project_context",
            lambda cwd=None: {"id": "7e2b0f41-9c6d-4a15-8b3e-5d0a9f2c6e71"},
        )

        assert _sync_user_templates_to_db(temp_db) == 1
        assert _rule_scope(temp_db, "project-rule") == "global"
