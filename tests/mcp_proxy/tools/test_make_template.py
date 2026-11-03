"""Tests for auto-export to YAML and make_global_template in MCP tools."""

import json
from pathlib import Path

import pytest

from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.template_writer import read_template


@pytest.fixture()
def manager(temp_db: HubDatabase) -> RuleDefinitionManager:
    return RuleDefinitionManager(temp_db)


def _create_rule_row(
    manager: RuleDefinitionManager,
    name: str,
    *,
    tags: list[str] | None = None,
    project_id: str | None = None,
) -> RuleDefinitionRow:
    tags = tags or ["user"]
    definition = {
        "event": "before_tool",
        "effects": [{"type": "inject_context", "template": "test"}],
    }
    return manager.create(
        name=name,
        definition_json=json.dumps(definition),
        source="installed",
        tags=tags,
        project_id=project_id,
    )


class TestAutoExportProjectRule:
    """Auto-export project-scoped definitions to .gobby/workflows/."""

    def test_export_creates_yaml(self, manager: RuleDefinitionManager, tmp_path: Path) -> None:
        from gobby.workflows.template_writer import write_rule_template

        row = _create_rule_row(manager, "my-custom-rule")
        definition = row.definition_json

        path = write_rule_template(
            name=row.name,
            definition=definition,
            output_dir=tmp_path / "rules",
            tags=["user"],
        )

        assert path.exists()
        data = read_template(path)
        assert "my-custom-rule" in data["rules"]
        assert data["tags"] == ["user"]

    def test_dev_mode_skips_export(self, tmp_path: Path) -> None:
        """In dev mode, auto-export should be skipped."""
        from gobby.utils.dev import is_dev_mode

        # Create a fake gobby project
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')

        assert is_dev_mode(tmp_path) is True

    def test_global_template_export(self, manager: RuleDefinitionManager, tmp_path: Path) -> None:
        """make_global_template writes to ~/.gobby/workflows/."""
        from gobby.workflows.template_writer import write_rule_template

        row = _create_rule_row(manager, "global-rule")
        definition = row.definition_json

        path = write_rule_template(
            name=row.name,
            definition=definition,
            output_dir=tmp_path / "global" / "rules",
            tags=["user"],
        )

        assert path.exists()
        data = read_template(path)
        assert "global-rule" in data["rules"]


class TestNameCollisionOnExport:
    """User exports should not overwrite gobby templates."""

    def test_rejects_gobby_named_export(self, temp_db: HubDatabase) -> None:
        """Cannot create user rule with name matching a gobby template."""
        from gobby.storage.definitions.rules import RuleDefinitionManager

        RuleDefinitionManager(temp_db).create(
            name="protected-rule",
            definition_json={
                "event": "before_tool",
                "effects": [{"type": "inject_context", "template": "x"}],
            },
            source="installed",
            tags=["gobby"],
        )

        from gobby.mcp_proxy.tools.workflows._auto_export import has_gobby_name_collision

        assert has_gobby_name_collision(temp_db, "protected-rule", kind="rule") is True
        assert has_gobby_name_collision(temp_db, "unique-user-rule", kind="rule") is False


class TestDeleteSyncsToDisk:
    """Deleting a definition should remove its YAML file."""

    def test_delete_removes_yaml(self, tmp_path: Path) -> None:
        from gobby.workflows.template_writer import delete_template_file, write_rule_template

        write_rule_template(
            name="doomed-rule",
            definition={
                "event": "before_tool",
                "effects": [{"type": "inject_context", "template": "x"}],
            },
            output_dir=tmp_path / "rules",
        )
        assert (tmp_path / "rules" / "doomed-rule.yaml").exists()

        deleted = delete_template_file("doomed-rule", tmp_path / "rules")
        assert deleted is True
        assert not (tmp_path / "rules" / "doomed-rule.yaml").exists()

    def test_update_overwrites_yaml(self, tmp_path: Path) -> None:
        from gobby.workflows.template_writer import write_rule_template

        write_rule_template(
            name="evolving-rule",
            definition={
                "event": "before_tool",
                "effects": [{"type": "inject_context", "template": "v1"}],
            },
            output_dir=tmp_path / "rules",
        )

        write_rule_template(
            name="evolving-rule",
            definition={
                "event": "before_tool",
                "effects": [{"type": "inject_context", "template": "v2"}],
            },
            output_dir=tmp_path / "rules",
        )

        data = read_template(tmp_path / "rules" / "evolving-rule.yaml")
        assert data["rules"]["evolving-rule"]["effects"][0]["template"] == "v2"
